from __future__ import absolute_import, division, print_function, unicode_literals

import time, os, codecs, json

from utils.tools import DatasetGenerator, write_result, create_masks
from utils.CustomSchedule import CustomSchedule
from utils.EarlystopHelper import EarlystopHelper
from utils.ReshuffleHelper import ReshuffleHelper
from utils.Metrics import MAE, MAPE
from models import STSAN_XL

import tensorflow as tf

import parameters_nyctaxi
import parameters_nycbike

gpus = tf.config.experimental.list_physical_devices('GPU')

if gpus:
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
    except RuntimeError as e:
        print(e)

class ModelTrainer:
    def __init__(self, model_index, args):
        assert args.dataset == 'taxi' or args.dataset == 'bike'

        """ use mirrored strategy for distributed training """
        self.strategy = tf.distribute.MirroredStrategy()
        strategy = self.strategy
        print('Number of GPU devices: {}'.format(strategy.num_replicas_in_sync))

        self.model_index = model_index
        self.args = args
        self.args.seq_len = (args.n_hist_week + args.n_hist_day) * args.n_hist_int + args.n_curr_int
        if args.weight_1:
            self.args.weight_2 = 1 - args.weight_1
        else:
            self.args.weight_2 = None
        self.GLOBAL_BATCH_SIZE = args.BATCH_SIZE * strategy.num_replicas_in_sync
        self.dataset_generator = DatasetGenerator(args.d_model,
                                                  args.dataset,
                                                  self.GLOBAL_BATCH_SIZE,
                                                  args.n_hist_week,
                                                  args.n_hist_day,
                                                  args.n_hist_int,
                                                  args.n_curr_int,
                                                  args.n_int_before,
                                                  args.n_pred,
                                                  args.local_block_len,
                                                  args.local_block_len_g,
                                                  args.pre_shuffle,
                                                  args.test_model)

        if args.dataset == 'taxi':
            self.f_max = parameters_nyctaxi.f_train_max
            self.es_patiences = [5, args.es_patience]
            self.es_threshold = args.es_threshold
            self.reshuffle_threshold = [2.0]
            self.test_threshold = 10 / self.f_max
        else:
            self.f_max = parameters_nycbike.f_train_max
            self.es_patiences = [5, args.es_patience]
            self.es_threshold = args.es_threshold
            self.reshuffle_threshold = [2.0]
            self.test_threshold = 10 / self.f_max

    def train(self):
        strategy = self.strategy
        args = self.args
        test_model = args.test_model
        result_output_path = "results/stsan_xl/{}.txt".format(self.model_index)

        train_dataset, val_dataset = self.dataset_generator.build_dataset('train', args.load_saved_data, strategy,
                                                                          args.st_revert, args.no_save)
        test_dataset = self.dataset_generator.build_dataset('test', args.load_saved_data, strategy,
                                                            args.st_revert, args.no_save)

        with strategy.scope():

            def tf_summary_scalar(summary_writer, name, value, step):
                with summary_writer.as_default():
                    tf.summary.scalar(name, value, step=step)

            def print_verbose(epoch, final_test):
                if final_test:
                    template_rmse = "RMSE(in/out):"
                    template_mae = "MAE(in/out):"
                    template_mape = "MAPE(in/out):"
                    for i in range(args.n_pred):
                        template_rmse += ' {}. {:.2f}({:.6f})/{:.2f}({:.6f})'.format(
                            i + 1,
                            in_rmse_test[i].result() * self.f_max,
                            in_rmse_test[i].result(),
                            out_rmse_test[i].result() * self.f_max,
                            out_rmse_test[i].result()
                        )
                        template_mae += ' {}. {:.2f}({:.6f})/{:.2f}({:.6f})'.format(
                            i + 1,
                            in_mae_test[i].result() * self.f_max,
                            in_mae_test[i].result(),
                            out_mae_test[i].result() * self.f_max,
                            out_mae_test[i].result()
                        )
                        template_mape += ' {}. {:.2f}/{:.2f}'.format(
                            i + 1,
                            in_mape_test[i].result(),
                            out_mape_test[i].result()
                        )
                    template = "Final:\n" + template_rmse + "\n" + template_mae + "\n" + template_mape
                    write_result(result_output_path, template)
                else:
                    template = "Epoch {} RMSE(in/out):".format(epoch + 1)
                    for i in range(args.n_pred):
                        template += " {}. {:.6f}/{:.6f}".format \
                            (i + 1, in_rmse_test[i].result(), out_rmse_test[i].result())
                    template += "\n"
                    write_result(result_output_path,
                                 'Validation Result (Min-Max Norm, filtering out trivial grids):\n' + template)

            loss_object = tf.keras.losses.MeanSquaredError(reduction=tf.keras.losses.Reduction.NONE)

            def loss_function(real, pred):
                loss_ = loss_object(real, pred)
                return tf.nn.compute_average_loss(loss_, global_batch_size=self.GLOBAL_BATCH_SIZE)

            in_rmse_train = tf.keras.metrics.RootMeanSquaredError(dtype=tf.float32)
            out_rmse_train = tf.keras.metrics.RootMeanSquaredError(dtype=tf.float32)
            in_rmse_test = [tf.keras.metrics.RootMeanSquaredError(dtype=tf.float32) for _ in range(args.n_pred)]
            out_rmse_test = [tf.keras.metrics.RootMeanSquaredError(dtype=tf.float32) for _ in range(args.n_pred)]

            in_mae_test = [MAE() for _ in range(args.n_pred)]
            out_mae_test = [MAE() for _ in range(args.n_pred)]
            in_mape_test = [MAPE() for _ in range(args.n_pred)]
            out_mape_test = [MAPE() for _ in range(args.n_pred)]

            learning_rate = CustomSchedule(args.d_model, args.warmup_steps)

            optimizer = tf.keras.optimizers.Adam(learning_rate, beta_1=0.9, beta_2=0.98, epsilon=1e-9)

            stsan_xl = STSAN_XL(args.num_layers,
                                args.d_model,
                                args.num_heads,
                                args.dff,
                                args.cnn_layers,
                                args.cnn_filters,
                                args.seq_len,
                                args.dropout_rate)

            def train_step(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y):

                padding_mask_g, padding_mask, combined_mask = \
                    create_masks(inp_g[..., :2], inp_ft[..., :2], dec_inp_f)

                with tf.GradientTape() as tape:
                    predictions, _, _ = stsan_xl(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, True,
                                              padding_mask, padding_mask_g, combined_mask)
                    if not args.weight_1:
                        loss = loss_function(y, predictions)
                    else:
                        loss = loss_function(y[:, :1, :], predictions[:, :1, :]) * args.weight_1 + \
                               loss_function(y[:, 1:, :], predictions[:, 1:, :]) * args.weight_2

                gradients = tape.gradient(loss, stsan_xl.trainable_variables)
                optimizer.apply_gradients(zip(gradients, stsan_xl.trainable_variables))

                in_rmse_train(y[..., 0], predictions[..., 0])
                out_rmse_train(y[..., 1], predictions[..., 1])

                return loss

            @tf.function
            def distributed_train_step(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y):
                per_replica_losses = strategy.experimental_run_v2 \
                    (train_step, args=(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y,))

                return strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_losses, axis=None)

            def test_step(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y, final_test=False):
                targets = dec_inp_f[:, :1, :]
                for i in range(args.n_pred):
                    tar_inp_ex = dec_inp_ex[:, :i + 1, :]
                    padding_mask_g, padding_mask, combined_mask = \
                        create_masks(inp_g[..., :2], inp_ft[..., :2], targets)

                    predictions, _, _ = stsan_xl(inp_g, inp_ft, inp_ex, targets, tar_inp_ex, cors, cors_g, False,
                                              padding_mask, padding_mask_g, combined_mask)

                    """ here we filter out all nodes where their real flows are less than 10 """
                    real_in = y[:, i, 0]
                    real_out = y[:, i, 1]
                    pred_in = predictions[:, -1, 0]
                    pred_out = predictions[:, -1, 1]
                    mask_in = tf.where(tf.math.greater(real_in, self.test_threshold))
                    mask_out = tf.where(tf.math.greater(real_out, self.test_threshold))
                    masked_real_in = tf.gather_nd(real_in, mask_in)
                    masked_real_out = tf.gather_nd(real_out, mask_out)
                    masked_pred_in = tf.gather_nd(pred_in, mask_in)
                    masked_pred_out = tf.gather_nd(pred_out, mask_out)
                    in_rmse_test[i](masked_real_in, masked_pred_in)
                    out_rmse_test[i](masked_real_out, masked_pred_out)
                    if final_test:
                        in_mae_test[i](masked_real_in, masked_pred_in)
                        out_mae_test[i](masked_real_out, masked_pred_out)
                        in_mape_test[i](masked_real_in, masked_pred_in)
                        out_mape_test[i](masked_real_out, masked_pred_out)

                    targets = tf.concat([targets, predictions[:, -1:, :]], axis=-2)

            @tf.function
            def distributed_test_step(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y, final_test):
                return strategy.experimental_run_v2(test_step, args=(
                    inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y, final_test,))

            def evaluate(eval_dataset, epoch, verbose=1, final_test=False):
                for i in range(args.n_pred):
                    in_rmse_test[i].reset_states()
                    out_rmse_test[i].reset_states()

                for (batch, (inp, tar)) in enumerate(eval_dataset):

                    inp_g = inp["inp_g"]
                    inp_ft = inp["inp_ft"]
                    inp_ex = inp["inp_ex"]
                    dec_inp_f = inp["dec_inp_f"]
                    dec_inp_ex = inp["dec_inp_ex"]
                    cors = inp["cors"]
                    cors_g = inp["cors_g"]

                    y = tar["y"]

                    distributed_test_step(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y, final_test)

                if verbose:
                    print_verbose(epoch, final_test)

            """ Start training... """
            es_flag = False
            check_flag = False
            es_helper = EarlystopHelper(self.es_patiences, self.es_threshold)
            reshuffle_helper = ReshuffleHelper(args.es_patience, self.reshuffle_threshold)
            summary_writer = tf.summary.create_file_writer('/home/lxx/tensorboard/stsan_xl/{}'.format(self.model_index))
            step_cnt = 0
            last_epoch = 0

            checkpoint_path = "./checkpoints/stsan_xl/{}".format(self.model_index)

            ckpt = tf.train.Checkpoint(STSAN_XL=stsan_xl, optimizer=optimizer)

            ckpt_manager = tf.train.CheckpointManager(ckpt, checkpoint_path,
                                                      max_to_keep=(args.es_patience + 1))

            if os.path.isfile(checkpoint_path + '/ckpt_record.json'):
                with codecs.open(checkpoint_path + '/ckpt_record.json', encoding='utf-8') as json_file:
                    ckpt_record = json.load(json_file)

                last_epoch = ckpt_record['epoch']
                es_flag = ckpt_record['es_flag']
                check_flag = ckpt_record['check_flag']
                es_helper.load_ckpt(checkpoint_path)
                reshuffle_helper.load_ckpt(checkpoint_path)
                step_cnt = ckpt_record['step_cnt']

                ckpt.restore(ckpt_manager.checkpoints[-1])
                write_result(result_output_path, "Check point restored at epoch {}".format(last_epoch))

            write_result(result_output_path, "Start training...\n")

            for epoch in range(last_epoch, args.MAX_EPOCH + 1):

                if es_flag or epoch == args.MAX_EPOCH:
                    print("Early stoping...")
                    if es_flag:
                        ckpt.restore(ckpt_manager.checkpoints[0])
                    else:
                        ckpt.restore(ckpt_manager.checkpoints[es_helper.get_bestepoch() - epoch - 1])
                    print('Checkpoint restored!! At epoch {}\n'.format(es_helper.get_bestepoch()))
                    break

                start = time.time()

                in_rmse_train.reset_states()
                out_rmse_train.reset_states()

                for (batch, (inp, tar)) in enumerate(train_dataset):

                    inp_g = inp["inp_g"]
                    inp_ft = inp["inp_ft"]
                    inp_ex = inp["inp_ex"]
                    dec_inp_f = inp["dec_inp_f"]
                    dec_inp_ex = inp["dec_inp_ex"]
                    cors = inp["cors"]
                    cors_g = inp["cors_g"]

                    y = tar["y"]

                    if args.trace_graph:
                        tf.summary.trace_on(graph=True, profiler=True)
                    total_loss = distributed_train_step(inp_g, inp_ft, inp_ex, dec_inp_f, dec_inp_ex, cors, cors_g, y)
                    if args.trace_graph:
                        with summary_writer.as_default():
                            tf.summary.trace_export(
                                name="stsan_xl_trace",
                                step=step_cnt,
                                profiler_outdir='/home/lxx/tensorboard/stsan_xl/{}'.format(self.model_index))

                    step_cnt += 1
                    tf_summary_scalar(summary_writer, "total_loss", total_loss, step_cnt)

                    if (batch + 1) % 100 == 0 and args.verbose_train:
                        print('Epoch {} Batch {} in_rmse {:.6f} out_rmse {:.6f}'.format(
                            epoch + 1, batch + 1, in_rmse_train.result(), out_rmse_train.result()))

                if args.verbose_train:
                    template = 'Epoch {} in_RMSE {:.6f} out_RMSE {:.6f}\n'.format \
                        (epoch + 1, in_rmse_train.result(), out_rmse_train.result())
                    write_result(result_output_path, template)
                    tf_summary_scalar(summary_writer, "in_rmse_train", in_rmse_train.result(), epoch + 1)
                    tf_summary_scalar(summary_writer, "out_rmse_train", out_rmse_train.result(), epoch + 1)

                eval_rmse = float(((in_rmse_train.result() + out_rmse_train.result()) / 2).numpy())

                if not check_flag and es_helper.refresh_status(eval_rmse):
                    check_flag = True

                if test_model or check_flag:
                    evaluate(val_dataset, epoch, final_test=False)
                    tf_summary_scalar(summary_writer, "in_rmse_test", in_rmse_test[0].result(), epoch + 1)
                    tf_summary_scalar(summary_writer, "out_rmse_test", out_rmse_test[0].result(), epoch + 1)
                    es_flag = es_helper.check(float((in_rmse_test[0].result() + out_rmse_test[0].result()).numpy()), epoch)
                    tf_summary_scalar(summary_writer, "best_epoch", es_helper.get_bestepoch(), epoch + 1)
                    if args.always_test and (epoch + 1) % args.always_test == 0:
                        write_result(result_output_path, "Always Test:")
                        evaluate(test_dataset, epoch)

                if test_model or reshuffle_helper.check(epoch):
                    train_dataset, val_dataset = \
                        self.dataset_generator.build_dataset('train', args.load_saved_data, strategy,
                                                             args.st_revert, args.no_save)

                ckpt_save_path = ckpt_manager.save()
                ckpt_record = {'epoch': epoch + 1, 'best_epoch': es_helper.get_bestepoch(),
                               'check_flag': check_flag, 'es_flag': es_flag, 'step_cnt': step_cnt}
                ckpt_record = json.dumps(ckpt_record, indent=4)
                with codecs.open(checkpoint_path + '/ckpt_record.json', 'w', 'utf-8') as outfile:
                    outfile.write(ckpt_record)
                es_helper.save_ckpt(checkpoint_path)
                reshuffle_helper.save_ckpt(checkpoint_path)
                print('Save checkpoint for epoch {} at {}\n'.format(epoch + 1, ckpt_save_path))

                tf_summary_scalar(summary_writer, "epoch_time", time.time() - start, epoch + 1)
                print('Time taken for 1 epoch: {} secs\n'.format(time.time() - start))

                if test_model:
                    es_flag = True

            write_result(result_output_path, "Start testing (filtering out trivial grids):")
            evaluate(test_dataset, epoch, final_test=True)
            tf_summary_scalar(summary_writer, "final_in_rmse", in_rmse_test[0].result(), 1)
            tf_summary_scalar(summary_writer, "final_out_rmse", out_rmse_test[0].result(), 1)

#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
<<FNN: Deep Learning over Multi-Field Categorical Data: A Case Study on User Response Prediction.>>
<<PNN: Product-based Neural Networks for User Response Prediction.>>
Implementation of FNN/PNN model with the following features：
#1 Input pipeline using Dataset API, Support parallel and prefetch.
#2 Train pipeline using Custom Estimator by rewriting model_fn.
#3 Support distributed training by TF_CONFIG.
#4 Support export_model for TensorFlow Serving.
########## TF Version: 1.8.0/Python Version: 3.6 ##########
"""

import os
import json
import glob
import random
import shutil
import tensorflow as tf
from datetime import date, timedelta

# =================== CMD Arguments for FNN/PNN model =================== #
flags = tf.app.flags
flags.DEFINE_integer("run_mode", 0, "{0-local, 1-single_distributed, 2-multi_distributed}")
flags.DEFINE_string("ps_hosts", None, "Comma-separated list of hostname:port pairs")
flags.DEFINE_string("worker_hosts", None, "Comma-separated list of hostname:port pairs")
flags.DEFINE_string("job_name", None, "Job name: ps or worker")
flags.DEFINE_integer("task_index", None, "Index of task within the job")
flags.DEFINE_integer("num_thread", 4, "Number of threads")
flags.DEFINE_string("input_dir", "", "Input data dir")
flags.DEFINE_string("model_dir", "", "Model check point file dir")
flags.DEFINE_string("file_name", "", "File for save model")
flags.DEFINE_string("algorithm", "OPNN", "Algorithm type {FNN, IPNN, OPNN}")
flags.DEFINE_string("task_mode", "train", "{train, eval, infer, export}")
flags.DEFINE_string("serve_dir", "", "Export servable model for TensorFlow Serving")
flags.DEFINE_boolean("clr_mode", True, "Clear existed model or not")
flags.DEFINE_integer("feature_size", 1842, "Number of features[numeric + one-hot categorical_feature]")
flags.DEFINE_integer("field_size", 39, "Number of fields")
flags.DEFINE_integer("embed_size", 10, "Embedding size[length of hidden vector of xi/xj]")
flags.DEFINE_integer("num_epochs", 10, "Number of epochs")
flags.DEFINE_integer("batch_size", 128, "Number of batch size")
flags.DEFINE_integer("log_steps", 1406, "Save summary every steps")
flags.DEFINE_string("loss_mode", "log_loss", "{log_loss, square_loss}")
flags.DEFINE_string("optimizer", "Adam", "{Adam, Adagrad, Momentum, Ftrl, GD}")
flags.DEFINE_float("learning_rate", 0.0005, "Learning rate")
flags.DEFINE_float("l2_reg_lambda", 0.0001, "L2 regularization")
flags.DEFINE_string("deep_layers", "256,128,64", "Deep layers")
flags.DEFINE_string("dropout", "0.5,0.5,0.5", "Dropout rate")
FLAGS = flags.FLAGS


# 0 1:0.1 2:0.003322 3:0.44 4:0.02 5:0.001594 6:0.016 7:0.02
# 8:0.04 9:0.008 10:0.166667 11:0.1 12:0 13:0.08
# 16:1 54:1 77:1 93:1 112:1 124:1 128:1 148:1 160:1 162:1 176:1 209:1 227:1
# 264:1 273:1 312:1 335:1 387:1 395:1 404:1 407:1 427:1 434:1 443:1 466:1 479:1
def input_fn(filenames, batch_size=64, num_epochs=1, perform_shuffle=True):
    print("Parsing ----------- ", filenames)

    def dataset_etl(line):
        feat_raw = tf.string_split([line], " ")
        labels = tf.string_to_number(feat_raw.values[0], out_type=tf.float32)
        splits = tf.string_split(feat_raw.values[1:], ":")
        idx_val = tf.reshape(splits.values, splits.dense_shape)
        feat_idx, feat_val = tf.split(idx_val, num_or_size_splits=2, axis=1)    # 切割张量
        feat_idx = tf.string_to_number(feat_idx, out_type=tf.int32)             # [field_size, 1]
        feat_val = tf.string_to_number(feat_val, out_type=tf.float32)           # [field_size, 1]
        return {"feat_idx": feat_idx, "feat_val": feat_val}, labels

    # extract lines from input files[filename or filename list] using the Dataset API,
    # multi-thread pre-process then prefetch some certain amount of data[6400]
    dataset = tf.data.TextLineDataset(filenames).map(dataset_etl, num_parallel_calls=4).prefetch(6400)

    # randomize the input data with a window of 512 elements (read into memory)
    if perform_shuffle:
        dataset = dataset.shuffle(buffer_size=512)

    # epochs from blending together
    dataset = dataset.batch(batch_size)
    dataset = dataset.repeat(num_epochs)
    iterator = dataset.make_one_shot_iterator()
    batch_features, batch_labels = iterator.get_next()      # [batch_size, field_size, 1]

    return batch_features, batch_labels


def model_fn(features, labels, mode, params):

    # ---------- hyper-parameters ---------- #
    algorithm = params["algorithm"]
    feature_size = params["feature_size"]
    field_size = params["field_size"]
    embed_size = params["embed_size"]
    learning_rate = params["learning_rate"]
    l2_reg_lambda = params["l2_reg_lambda"]
    layers = list(map(int, params["deep_layers"].split(',')))       # l1神经元数量等于D1长度
    dropout = list(map(float, params["dropout"].split(',')))
    num_pairs = int(field_size * (field_size - 1) / 2)

    # ---------- initial weights ----------- #
    # [numeric_feature, one-hot categorical_feature]统一做embedding
    coe_b = tf.get_variable(name="coe_b", shape=[1], initializer=tf.constant_initializer(0.0))
    coe_w = tf.get_variable(name="coe_w", shape=[feature_size], initializer=tf.glorot_normal_initializer())
    coe_v = tf.get_variable(name="coe_v", shape=[feature_size, embed_size],
                            initializer=tf.glorot_normal_initializer())
    coe_line = tf.get_variable(name="coe_line", shape=[layers[0], field_size, embed_size],
                               initializer=tf.glorot_normal_initializer())
    coe_ipnn = tf.get_variable(name="coe_ipnn", shape=[layers[0], field_size],
                               initializer=tf.glorot_normal_initializer())
    coe_opnn = tf.get_variable(name="coe_opnn", shape=[layers[0], embed_size, embed_size],
                               initializer=tf.glorot_normal_initializer())

    # ---------- reshape feature ----------- #
    feat_idx = features["feat_idx"]         # 非零特征位置[batch_size, field_size, 1]
    feat_idx = tf.reshape(feat_idx, shape=[-1, field_size])     # [Batch, Field]
    feat_val = features["feat_val"]         # 非零特征的值[batch_size, field_size, 1]
    feat_val = tf.reshape(feat_val, shape=[-1, field_size])     # [Batch, Field]

    # ------------- define f(x) ------------ #
    with tf.variable_scope("Linear-Part"):
        feat_wgt = tf.nn.embedding_lookup(coe_w, feat_idx)              # [Batch, Field]
        y_linear = tf.reduce_sum(tf.multiply(feat_wgt, feat_val), 1)    # [Batch]

    with tf.variable_scope("Embed-Layer"):
        embeddings = tf.nn.embedding_lookup(coe_v, feat_idx)            # [Batch, Field, K]
        feat_vals = tf.reshape(feat_val, shape=[-1, field_size, 1])     # [Batch, Field, 1]
        embeddings = tf.multiply(embeddings, feat_vals)                 # [Batch, Field, K]

    with tf.variable_scope("Product-Layer"):
        if algorithm == "FNN":
            feat_vec = tf.reshape(embeddings, shape=[-1, field_size*embed_size])
            feat_bias = coe_b * tf.reshape(tf.ones_like(y_linear, dtype=tf.float32), shape=[-1, 1])
            deep_inputs = tf.concat([feat_wgt, feat_vec, feat_bias], 1)     # [Batch, (Field+1)*K+1]
        elif algorithm == "IPNN":
            # linear signal
            z = tf.reshape(embeddings, shape=[-1, field_size*embed_size])   # [Batch, Field*K]
            wz = tf.reshape(coe_line, shape=[-1, field_size*embed_size])    # [D1, Field*K]
            lz = tf.matmul(z, tf.transpose(wz))                             # [Batch, D1]

            # quadratic signal
            row_i = []
            col_j = []
            for i in range(field_size - 1):
                for j in range(i + 1, field_size):
                    row_i.append(i)
                    col_j.append(j)
            fi = tf.gather(embeddings, row_i, axis=1)           # 根据索引从参数轴上收集切片[Batch, num_pairs, K]
            fj = tf.gather(embeddings, col_j, axis=1)           # 根据索引从参数轴上收集切片[Batch, num_pairs, K]

            # p_ij = g(fi,fj)=<fi,fj> 特征i和特征j的隐向量的内积
            p = tf.reduce_sum(tf.multiply(fi, fj), 2)           # p矩阵展成向量[Batch, num_pairs]
            wpi = tf.gather(coe_ipnn, row_i, axis=1)            # 根据索引从参数轴上收集切片[D1, num_pairs]
            wpj = tf.gather(coe_ipnn, col_j, axis=1)            # 根据索引从参数轴上收集切片[D1, num_pairs]
            wp = tf.multiply(wpi, wpj)                          # D1个W矩阵组成的矩阵(每行代表一个W)[D1, num_pairs]
            lp = tf.matmul(p, tf.transpose(wp))                 # [Batch, D1]

            lb = coe_b * tf.reshape(tf.ones_like(y_linear, dtype=tf.float32), shape=[-1, 1])
            deep_inputs = lz + lp + lb                          # [Batch, D1]
        elif algorithm == "OPNN":
            # linear signal
            z = tf.reshape(embeddings, shape=[-1, field_size*embed_size])   # [Batch, Field*K]
            wz = tf.reshape(coe_line, shape=[-1, field_size*embed_size])    # [D1, Field*K]
            lz = tf.matmul(z, tf.transpose(wz))                             # [Batch, D1]

            # quadratic signal
            f_sigma = tf.reduce_sum(embeddings, axis=1)                     # [Batch, K]
            p = tf.matmul(tf.reshape(f_sigma, shape=[-1, embed_size, 1]),
                          tf.reshape(f_sigma, shape=[-1, 1, embed_size]))   # [Batch, K, K]
            p = tf.reshape(p, shape=[-1, embed_size*embed_size])            # [Batch, K*K]
            wp = tf.reshape(coe_opnn, shape=[-1, embed_size*embed_size])    # [D1, K*K]
            lp = tf.matmul(p, tf.transpose(wp))                             # [Batch, D1]

            lb = coe_b * tf.reshape(tf.ones_like(y_linear, dtype=tf.float32), shape=[-1, 1])
            deep_inputs = lz + lp + lb                                      # [Batch, D1]

    with tf.variable_scope("Deep-Layer"):
        # hidden layer
        for i in range(len(layers)):
            deep_inputs = tf.contrib.layers.fully_connected(
                inputs=deep_inputs, num_outputs=layers[i], scope="mlp_%d" % i,
                weights_regularizer=tf.contrib.layers.l2_regularizer(l2_reg_lambda))
            if mode == tf.estimator.ModeKeys.TRAIN:
                deep_inputs = tf.nn.dropout(deep_inputs, keep_prob=dropout[i])

        # output layer
        y_d = tf.contrib.layers.fully_connected(
            inputs=deep_inputs, num_outputs=1, activation_fn=tf.identity,
            weights_regularizer=tf.contrib.layers.l2_regularizer(l2_reg_lambda), scope='deep_out')

    with tf.variable_scope("FPNN-Out"):
        y_hat = tf.reshape(y_d, shape=[-1])
        y_pred = tf.nn.sigmoid(y_hat)

    # ----- mode: predict/evaluate/train ----- #
    # predict: 不计算loss/metric; evaluate: 不进行梯度下降和参数更新

    # Provide an estimator spec for 'ModeKeys.PREDICT'
    predictions = {"prob": y_pred}
    export_outputs = {
        tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
            tf.estimator.export.PredictOutput(predictions)}
    if mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions, export_outputs=export_outputs)

    # Provide an estimator spec for 'ModeKeys.EVAL'
    if FLAGS.loss_mode == "log_loss":
        loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(labels=labels, logits=y_hat)) +\
               l2_reg_lambda * tf.nn.l2_loss(coe_w) + l2_reg_lambda * tf.nn.l2_loss(coe_v)
    else:
        loss = tf.reduce_mean(tf.square(labels-y_pred))
    eval_metric_ops = {"auc": tf.metrics.auc(labels, y_pred)}
    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions, loss=loss,
                                          eval_metric_ops=eval_metric_ops)

    # Provide an estimator spec for 'ModeKeys.TRAIN'
    if FLAGS.optimizer == "Adam":
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate, beta1=0.9, beta2=0.999, epsilon=1e-8)
    elif FLAGS.optimizer == "Adagrad":
        optimizer = tf.train.AdagradOptimizer(learning_rate=learning_rate, initial_accumulator_value=1e-8)
    elif FLAGS.optimizer == "Momentum":
        optimizer = tf.train.MomentumOptimizer(learning_rate=learning_rate, momentum=0.95)
    elif FLAGS.optimizer == "Ftrl":
        optimizer = tf.train.FtrlOptimizer(learning_rate)
    else:
        optimizer = tf.train.GradientDescentOptimizer(learning_rate=learning_rate)
    train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())

    if mode == tf.estimator.ModeKeys.TRAIN:
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions, loss=loss, train_op=train_op)


# Initialized Distributed Environment,初始化分布式环境
def distributed_env_set():
    if FLAGS.run_mode == 1:         # 单机分布式
        ps_hosts = FLAGS.ps_hosts.split(',')
        chief_hosts = FLAGS.worker_hosts.split(',')
        job_name = FLAGS.job_name
        task_index = FLAGS.task_index
        print('ps_host --------', ps_hosts)
        print('chief_hosts ----', chief_hosts)
        print('job_name -------', job_name)
        print('task_index -----', str(task_index))
        # 无worker参数
        tf_config = {
            'cluster': {'chief': chief_hosts, 'ps': ps_hosts},
            'task': {'type': job_name, 'index': task_index}
        }
        print(json.dumps(tf_config))
        os.environ['TF_CONFIG'] = json.dumps(tf_config)
    elif FLAGS.run_mode == 2:      # 集群分布式
        ps_hosts = FLAGS.ps_hosts.split(',')
        worker_hosts = FLAGS.worker_hosts.split(',')
        chief_hosts = worker_hosts[0:1]     # get first worker as chief
        worker_hosts = worker_hosts[1:]     # the rest as worker
        task_index = FLAGS.task_index
        job_name = FLAGS.job_name
        print('ps_host ------', ps_hosts)
        print('worker_host --', worker_hosts)
        print('chief_hosts --', chief_hosts)
        print('job_name -----', job_name)
        print('task_index ---', str(task_index))
        # use #worker=0 as chief
        if job_name == "worker" and task_index == 0:
            job_name = "chief"
        # use #worker=1 as evaluator
        if job_name == "worker" and task_index == 1:
            job_name = 'evaluator'
            task_index = 0
        # the others as worker
        if job_name == "worker" and task_index > 1:
            task_index -= 2

        tf_config = {
            'cluster': {'chief': chief_hosts, 'worker': worker_hosts, 'ps': ps_hosts},
            'task': {'type': job_name, 'index': task_index}
        }
        print(json.dumps(tf_config))
        os.environ['TF_CONFIG'] = json.dumps(tf_config)


# print initial information of paras,打印初始化参数信息
def _print_init_info(train_files, valid_files, tests_files):
    print("input_dir ---------- ", FLAGS.input_dir)
    print("model_dir ---------- ", FLAGS.model_dir)
    print("file_name ---------- ", FLAGS.file_name)
    print("algorithm ---------- ", FLAGS.algorithm)
    print("task_mode ---------- ", FLAGS.task_mode)
    print("feature_size ------- ", FLAGS.feature_size)
    print("field_size --------- ", FLAGS.field_size)
    print("embed_size --------- ", FLAGS.embed_size)
    print("num_epochs --------- ", FLAGS.num_epochs)
    print("batch_size --------- ", FLAGS.batch_size)
    print("loss_mode ---------- ", FLAGS.loss_mode)
    print("optimizer ---------- ", FLAGS.optimizer)
    print("learning_rate ------ ", FLAGS.learning_rate)
    print("l2_reg_lambda ------ ", FLAGS.l2_reg_lambda)
    print("deep_layers -------- ", FLAGS.deep_layers)
    print("dropout ------------ ", FLAGS.dropout)
    print("train_files: ", train_files)
    print("valid_files: ", valid_files)
    print("tests_files: ", tests_files)


def main(_):
    print("==================== 1.Check Args and Initialized Distributed Env...")
    if FLAGS.file_name == "":       # 存储算法模型文件名称[标记不同时刻训练模型,程序执行日期前一天:20190327]
        FLAGS.file_name = "ch05_FNN_PNN_" + (date.today() + timedelta(-1)).strftime('%Y%m%d')
    FLAGS.model_dir = FLAGS.model_dir + FLAGS.file_name
    if FLAGS.input_dir == "":       # windows环境测试[未指定data目录条件下]
        root_dir = os.path.dirname(os.path.dirname(os.getcwd()))
        FLAGS.input_dir = root_dir + "\\data" + "\\criteo_data_set\\"

    train_files = glob.glob("%s/train*set" % FLAGS.input_dir)       # 获取指定目录下train文件
    random.shuffle(train_files)                                     # 打散train文件
    valid_files = glob.glob("%s/valid*set" % FLAGS.input_dir)       # 获取指定目录下valid文件
    tests_files = glob.glob("%s/tests*set" % FLAGS.input_dir)       # 获取指定目录下tests文件
    _print_init_info(train_files, valid_files, tests_files)

    if FLAGS.clr_mode and FLAGS.task_mode == "train":               # 删除已存在的模型文件
        try:
            shutil.rmtree(FLAGS.model_dir)      # 递归删除目录下的目录及文件
        except Exception as e:
            print(e, "At clear_existed_model")
        else:
            print("Existed model cleared at %s folder" % FLAGS.model_dir)
    distributed_env_set()       # 分布式环境设置

    print("==================== 2.Set model params and Build FNN/PNN model...")
    model_params = {
        "feature_size": FLAGS.feature_size,
        "field_size": FLAGS.field_size,
        "embed_size": FLAGS.embed_size,
        "learning_rate": FLAGS.learning_rate,
        "l2_reg_lambda": FLAGS.l2_reg_lambda,
        "deep_layers": FLAGS.deep_layers,
        "dropout": FLAGS.dropout,
        "algorithm": FLAGS.algorithm
    }
    session_config = tf.ConfigProto(device_count={'GPU': 1, 'CPU': FLAGS.num_thread})
    config = tf.estimator.RunConfig().replace(session_config=session_config,
                                              save_summary_steps=FLAGS.log_steps,
                                              log_step_count_steps=FLAGS.log_steps)
    fpnn = tf.estimator.Estimator(model_fn=model_fn, model_dir=FLAGS.model_dir,
                                  params=model_params, config=config)

    print("==================== 3.Apply FNN/PNN model to diff tasks...")
    train_step = 179968*FLAGS.num_epochs/FLAGS.batch_size       # data_num * num_epochs / batch_size
    if FLAGS.task_mode == "train":
        train_spec = tf.estimator.TrainSpec(
            input_fn=lambda: input_fn(train_files, batch_size=FLAGS.batch_size, num_epochs=FLAGS.num_epochs),
            max_steps=train_step)
        eval_spec = tf.estimator.EvalSpec(
            input_fn=lambda: input_fn(valid_files, batch_size=FLAGS.batch_size, num_epochs=1),
            steps=None, start_delay_secs=500, throttle_secs=600)
        tf.estimator.train_and_evaluate(fpnn, train_spec, eval_spec)
    elif FLAGS.task_mode == "eval":
        fpnn.evaluate(input_fn=lambda: input_fn(valid_files, batch_size=FLAGS.batch_size, num_epochs=1))
    elif FLAGS.task_mode == "infer":
        preds = fpnn.predict(
            input_fn=lambda: input_fn(tests_files, batch_size=FLAGS.batch_size, num_epochs=1),
            predict_keys="prob")
        with open(FLAGS.input_dir+"/tests_pred.txt", "w") as fo:
            for prob in preds:
                fo.write("%f\n" % (prob['prob']))
    elif FLAGS.task_mode == "export":
        feature_spec = {
            "feat_idx": tf.placeholder(dtype=tf.int64, shape=[None, FLAGS.field_size], name="feat_idx"),
            "feat_val": tf.placeholder(dtype=tf.float32, shape=[None, FLAGS.field_size], name="feat_val")}
        serving_input_receiver_fn = tf.estimator.export.build_raw_serving_input_receiver_fn(feature_spec)
        fpnn.export_savedmodel(FLAGS.serve_dir, serving_input_receiver_fn)


if __name__ == "__main__":
    tf.logging.set_verbosity(tf.logging.INFO)
    tf.app.run()

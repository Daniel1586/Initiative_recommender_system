#!/usr/bin/python
# -*- coding: utf-8 -*-

import numpy as np
from time import time
import tensorflow as tf
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.metrics import roc_auc_score


class DeepFM(BaseEstimator, TransformerMixin):
    def __init__(self, feature_size, field_size, embedding_size=8,
                 dropout_fm=None, deep_layers=None, dropout_deep=None,
                 deep_layer_activation=tf.nn.relu, epoch=10, batch_size=256,
                 learning_rate=0.001, optimizer="adam", batch_norm=0,
                 batch_norm_decay=0.995, verbose=False, random_seed=2016,
                 use_fm=True, use_deep=True,
                 loss_type="logloss", eval_metric=roc_auc_score,
                 l2_reg=0.0, greater_is_better=True):
        assert (use_fm or use_deep)
        assert loss_type in ["logloss", "mse"],\
            "loss_type can be either 'logloss' for classification task or 'mse' for regression task"
        if dropout_fm is None:
            dropout_fm = [1.0, 1.0]
        if deep_layers is None:
            deep_layers = [32, 32]
        if dropout_deep is None:
            dropout_deep = [0.5, 0.5, 0.5]

        self.feature_size = feature_size
        self.field_size = field_size
        self.embedding_size = embedding_size

        self.dropout_fm = dropout_fm
        self.deep_layers = deep_layers
        self.dropout_dep = dropout_deep
        self.deep_layers_activation = deep_layer_activation
        self.use_fm = use_fm
        self.use_deep = use_deep
        self.l2_reg = l2_reg

        self.epoch = epoch
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.optimizer_type = optimizer

        self.batch_norm = batch_norm
        self.batch_norm_decay = batch_norm_decay

        self.verbose = verbose
        self.random_seed = random_seed
        self.loss_type = loss_type
        self.eval_metric = eval_metric
        self.greater_is_better = greater_is_better
        self.train_result, self.valid_result = [], []

        self._init_graph()

    def _init_graph(self):
        self.graph = tf.Graph()
        with self.graph.as_default():
            tf.set_random_seed(self.random_seed)

            # input data,模型输入
            self.feat_index = tf.placeholder(tf.int32, shape=[None, None],
                                             name='feat_index')
            self.feat_value = tf.placeholder(tf.float32, shape=[None, None],
                                             name='feat_value')
            self.label = tf.placeholder(tf.float32, shape=[None, 1], name='label')

            self.dropout_keep_fm = tf.placeholder(tf.float32, shape=[None],
                                                  name='dropout_keep_fm')
            self.dropout_keep_deep = tf.placeholder(tf.float32, shape=[None],
                                                    name='dropout_deep_deep')
            self.train_phase = tf.placeholder(tf.bool, name='train_phase')

            # weight initializing,权重初始化
            self.weights = self._initialize_weights()

            # model
            self.embeddings = tf.nn.embedding_lookup(self.weights['feature_embeddings'], self.feat_index)
            feat_value = tf.reshape(self.feat_value, shape=[-1, self.field_size, 1])
            self.embeddings = tf.multiply(self.embeddings, feat_value)

            # first order term
            self.y_first_order = tf.nn.embedding_lookup(self.weights['feature_bias'], self.feat_index)
            self.y_first_order = tf.reduce_sum(tf.multiply(self.y_first_order, feat_value), 2)
            self.y_first_order = tf.nn.dropout(self.y_first_order, self.dropout_keep_fm[0])

            # second order term
            # sum-square-part
            self.summed_features_emb = tf.reduce_sum(self.embeddings, 1)    # None * k
            self.summed_features_emb_square = tf.square(self.summed_features_emb)   # None * K

            # square-sum-part
            self.squared_features_emb = tf.square(self.embeddings)
            self.squared_sum_features_emb = tf.reduce_sum(self.squared_features_emb, 1)  # None * K

            # second order
            self.y_second_order = 0.5 * tf.subtract(self.summed_features_emb_square,
                                                    self.squared_sum_features_emb)
            self.y_second_order = tf.nn.dropout(self.y_second_order, self.dropout_keep_fm[1])

            # Deep component
            self.y_deep = tf.reshape(self.embeddings, shape=[-1, self.field_size * self.embedding_size])
            self.y_deep = tf.nn.dropout(self.y_deep, self.dropout_keep_deep[0])

            for i in range(0, len(self.deep_layers)):
                self.y_deep = tf.add(tf.matmul(self.y_deep,
                                               self.weights["layer_%d" % i]), self.weights["bias_%d" % i])
                self.y_deep = self.deep_layers_activation(self.y_deep)
                self.y_deep = tf.nn.dropout(self.y_deep, self.dropout_keep_deep[i+1])

            if self.use_fm and self.use_deep:   # DeepFM
                concat_input = tf.concat([self.y_first_order, self.y_second_order, self.y_deep], axis=1)
            elif self.use_fm:                   # FM
                concat_input = tf.concat([self.y_first_order, self.y_second_order], axis=1)
            elif self.use_deep:                 # Deep
                concat_input = self.y_deep

            self.out = tf.add(tf.matmul(concat_input, self.weights['concat_projection']),
                              self.weights['concat_bias'])

            # loss,代价函数
            if self.loss_type == "logloss":
                self.out = tf.nn.sigmoid(self.out)
                self.loss = tf.losses.log_loss(self.label, self.out)
            elif self.loss_type == "mse":
                self.loss = tf.nn.l2_loss(tf.subtract(self.label, self.out))
            # l2 regularization on weights
            if self.l2_reg > 0:
                self.loss += tf.contrib.layers.l2_regularizer(
                    self.l2_reg)(self.weights["concat_projection"])
                if self.use_deep:
                    for i in range(len(self.deep_layers)):
                        self.loss += tf.contrib.layers.l2_regularizer(
                            self.l2_reg)(self.weights["layer_%d" % i])

            # optimizer,优化器选择
            if self.optimizer_type == "adam":
                self.optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate, beta1=0.9, beta2=0.999,
                                                        epsilon=1e-8).minimize(self.loss)
            elif self.optimizer_type == "adagrad":
                self.optimizer = tf.train.AdagradOptimizer(learning_rate=self.learning_rate,
                                                           initial_accumulator_value=1e-8).minimize(self.loss)
            elif self.optimizer_type == "gd":
                self.optimizer = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate).minimize(self.loss)
            elif self.optimizer_type == "momentum":
                self.optimizer = tf.train.MomentumOptimizer(learning_rate=self.learning_rate,
                                                            momentum=0.95).minimize(self.loss)

            # init
            self.saver = tf.train.Saver()
            init = tf.global_variables_initializer()
            self.sess = tf.Session()
            self.sess.run(init)

            # number of params
            total_parameters = 0
            for variable in self.weights.values():
                shape = variable.get_shape()
                variable_parameters = 1
                for dim in shape:
                    variable_parameters *= dim.value
                total_parameters += variable_parameters
            if self.verbose > 0:
                print("#params: %d" % total_parameters)

    def _initialize_weights(self):
        weights = dict()

        # Sparse Features->Dense Embeddings weight initializing
        # one-hot编码后输入到Embedding的权重矩阵初始化
        weights['feature_embeddings'] = tf.Variable(tf.random_normal(
            [self.feature_size, self.embedding_size], 0.0, 0.01), name='feature_embeddings')
        weights['feature_bias'] = tf.Variable(tf.random_normal(
            [self.feature_size, 1], 0.0, 1.0), name='feature_bias')

        # Deep layers weight initializing,Xavier初始化
        num_layer = len(self.deep_layers)
        input_size = self.field_size * self.embedding_size
        glorot = np.sqrt(2.0/(input_size + self.deep_layers[0]))    # var(w)=2/(nin+nout)

        weights['layer_0'] = tf.Variable(np.random.normal(
            loc=0, scale=glorot, size=(input_size, self.deep_layers[0])), dtype=np.float32)
        weights['bias_0'] = tf.Variable(np.random.normal(
            loc=0, scale=glorot, size=(1, self.deep_layers[0])), dtype=np.float32)

        for i in range(1, num_layer):
            glorot = np.sqrt(2.0 / (self.deep_layers[i - 1] + self.deep_layers[i]))
            weights["layer_%d" % i] = tf.Variable(np.random.normal(
                loc=0, scale=glorot, size=(self.deep_layers[i - 1], self.deep_layers[i])),
                dtype=np.float32)  # layers[i-1] * layers[i]
            weights["bias_%d" % i] = tf.Variable(np.random.normal(
                loc=0, scale=glorot, size=(1, self.deep_layers[i])),
                dtype=np.float32)  # 1 * layer[i]

        # final concat projection layer,FM一次项,FM交叉项,Deep Hidden layer最后一层
        if self.use_fm and self.use_deep:
            input_size = self.field_size + self.embedding_size + self.deep_layers[-1]
        elif self.use_fm:
            input_size = self.field_size + self.embedding_size
        elif self.use_deep:
            input_size = self.deep_layers[-1]

        glorot = np.sqrt(2.0/(input_size + 1))
        weights['concat_projection'] = tf.Variable(np.random.normal(
            loc=0, scale=glorot, size=(input_size, 1)), dtype=np.float32)
        weights['concat_bias'] = tf.Variable(tf.constant(0.01), dtype=np.float32)

        return weights

    # noinspection PyMethodMayBeStatic
    def get_batch(self, xi, xv, y, batch_size, index):
        start = index * batch_size
        end = (index + 1) * batch_size
        end = end if end < len(y) else len(y)
        return xi[start:end], xv[start:end], [[y_] for y_ in y[start:end]]

    # noinspection PyMethodMayBeStatic
    def shuffle_in_unison_scary(self, a, b, c):
        rng_state = np.random.get_state()
        np.random.shuffle(a)
        np.random.set_state(rng_state)
        np.random.shuffle(b)
        np.random.set_state(rng_state)
        np.random.shuffle(c)

    def evaluate(self, xi, xv, y):
        """
        :param xi: list of list of feature indices of each sample in the dataset
        :param xv: list of list of feature values of each sample in the dataset
        :param y: label of each sample in the dataset
        :return: metric of the evaluation
        """
        y_pred = self.predict(xi, xv)
        return self.eval_metric(y, y_pred)

    def predict(self, xi, xv):
        """
        :param xi: list of list of feature indices of each sample in the dataset
        :param xv: list of list of feature values of each sample in the dataset
        :return: predicted probability of each sample
        """
        # dummy y
        dummy_y = [1] * len(xi)
        batch_index = 0
        xi_batch, xv_batch, y_batch = self.get_batch(xi, xv, dummy_y, self.batch_size, batch_index)
        y_pred = None
        while len(xi_batch) > 0:
            num_batch = len(y_batch)
            feed_dict = {self.feat_index: xi_batch,
                         self.feat_value: xv_batch,
                         self.label: y_batch,
                         self.dropout_keep_fm: [1.0] * len(self.dropout_fm),
                         self.dropout_keep_deep: [1.0] * len(self.dropout_dep),
                         self.train_phase: False}
            batch_out = self.sess.run(self.out, feed_dict=feed_dict)

            if batch_index == 0:
                y_pred = np.reshape(batch_out, (num_batch,))
            else:
                y_pred = np.concatenate((y_pred, np.reshape(batch_out, (num_batch,))))

            batch_index += 1
            xi_batch, xv_batch, y_batch = self.get_batch(xi, xv, dummy_y, self.batch_size, batch_index)

        return y_pred

    def fit_on_batch(self, xi, xv, y):
        feed_dict = {self.feat_index: xi,
                     self.feat_value: xv,
                     self.label: y,
                     self.dropout_keep_fm: self.dropout_fm,
                     self.dropout_keep_deep: self.dropout_dep,
                     self.train_phase: True}
        loss, opt = self.sess.run([self.loss, self.optimizer], feed_dict=feed_dict)
        return loss

    def fit(self, xi_train, xv_train, y_train, xi_valid=None, xv_valid=None,
            y_valid=None, early_stopping=False, refit=False):
        """
        :param xi_train: [[ind1_1, ind1_2, ...], ..., [indi_1, indi_2, ..., indi_j, ...], ...]
                         indi_j is the feature index of feature field j of sample i in the training set
        :param xv_train: [[val1_1, val1_2, ...], ..., [vali_1, vali_2, ..., vali_j, ...], ...]
                         vali_j is the feature value of feature field j of sample i in the training set
                         vali_j can be either binary (1/0, for binary/categorical features) or float
                         (e.g., 10.24, for numerical features)
        :param y_train: label of each sample in the training set
        :param xi_valid: list of list of feature indices of each sample in the validation set
        :param xv_valid: list of list of feature values of each sample in the validation set
        :param y_valid: label of each sample in the validation set
        :param early_stopping: perform early stopping or not
        :param refit: refit the model on the train+valid dataset or not
        :return: None
        """

        has_valid = xv_valid is not None
        for epoch in range(self.epoch):
            t1 = time()
            # shuffle the dataset,打乱dataset顺序
            self.shuffle_in_unison_scary(xi_train, xv_train, y_train)
            total_batch = int(len(y_train) / self.batch_size)
            # get batch data and fit them,获得batch数据并fit
            for i in range(total_batch):
                xi_batch, xv_batch, y_batch = self.get_batch(xi_train, xv_train, y_train, self.batch_size, i)
                self.fit_on_batch(xi_batch, xv_batch, y_batch)

            # evaluate training and validation dataset,评价train/valid dataset
            train_result = self.evaluate(xi_train, xv_train, y_train)
            self.train_result.append(train_result)

            if has_valid:
                valid_result = self.evaluate(xi_valid, xv_valid, y_valid)
                self.valid_result.append(valid_result)
            if self.verbose > 0 and epoch % self.verbose == 0:
                if has_valid:
                    print("[%d] train-result=%.4f, valid-result=%.4f [%.1f s]"
                          % (epoch + 1, train_result, valid_result, time() - t1))
                else:
                    print("[%d] train-result=%.4f [%.1f s]"
                          % (epoch + 1, train_result, time() - t1))
            if has_valid and early_stopping and self.training_termination(self.valid_result):
                break

        # fit a few more epoch on train+valid until result reaches the best_train_score
        if has_valid and refit:
            if self.greater_is_better:
                best_valid_score = max(self.valid_result)
            else:
                best_valid_score = min(self.valid_result)

            best_epoch = self.valid_result.index(best_valid_score)
            best_train_score = self.train_result[best_epoch]
            xi_train = xi_train + xi_valid
            xv_train = xv_train + xv_valid
            y_train = y_train + y_valid
            for epoch in range(100):
                self.shuffle_in_unison_scary(xi_train, xv_train, y_train)
                total_batch = int(len(y_train) / self.batch_size)
                for i in range(total_batch):
                    xi_batch, xv_batch, y_batch = self.get_batch(xi_train, xv_train, y_train,
                                                                 self.batch_size, i)
                    self.fit_on_batch(xi_batch, xv_batch, y_batch)
                # check the model performance
                train_result = self.evaluate(xi_train, xv_train, y_train)
                ckp1 = abs(train_result - best_train_score) < 0.001
                ckp2 = self.greater_is_better and train_result > best_train_score
                ckp3 = (not self.greater_is_better) and train_result < best_train_score
                if ckp1 or ckp2 or ckp3:
                    break

    def training_termination(self, valid_result):
        if len(valid_result) > 5:
            if self.greater_is_better:
                if valid_result[-1] < valid_result[-2] < valid_result[-3] < valid_result[-4] < valid_result[-5]:
                    return True
            else:
                if valid_result[-1] > valid_result[-2] > valid_result[-3] > valid_result[-4] > valid_result[-5]:
                    return True
        return False

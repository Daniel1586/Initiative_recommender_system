#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import numpy as np
import pandas as pd
import tensorflow as tf
from sklearn.model_selection import StratifiedKFold
from dataReader import FeatureDictionary, DataParser
from matplotlib import pyplot as plt

import config
from ProductNN import ProductNN


def _load_data():
    dftrain = pd.read_csv(config.TRAIN_FILE)    # 595212*59,有‘target’项
    dftests = pd.read_csv(config.TEST_FILE)     # 892816*58,无‘target’项

    def preprocess(df):
        cols_ = [c for c in df.columns if c not in ['id', 'target']]
        # 计算每个样本缺失值个数
        df["missed_feature_num"] = np.sum((df[cols_] == -1).values, axis=1)
        df['ps_car_13_x_ps_reg_03'] = df['ps_car_13'] * df['ps_reg_03']
        return df

    dftrain = preprocess(dftrain)
    dftests = preprocess(dftests)

    cols = [c for c in dftrain.columns if c not in ['id', 'target']]
    cols = [c for c in cols if c not in config.IGNORE_COLS]

    _x_train = dftrain[cols].values
    _y_train = dftrain['target'].values
    _x_tests = dftests[cols].values
    _id_test = dftests['id'].values
    cat_feature_index = [i for i, c in enumerate(cols) if c in config.CATEGORICAL_COLS]

    return dftrain, dftests, _x_train, _y_train, _x_tests, _id_test, cat_feature_index


def run_base_model_pnn(dftrain, dftests, folds, pnn_para):
    # dataset数据集处理,得到特征的有效key值和one-hot之后的特征个数
    fd = FeatureDictionary(dftrain=dftrain, dftests=dftests,
                           numeric_cols=config.NUMERIC_COLS,
                           ignore_cols=config.IGNORE_COLS)

    data_parser = DataParser(feat_dict=fd)
    # _xi_train: 非零特征位置;_xv_train:非零特征的值
    # _y_train: 训练数据label;_id_test: 测试数据id
    _xi_train, _xv_train, _y_train = data_parser.parse(df=dftrain, has_label=True)
    _xi_tests, _xv_tests, _id_test = data_parser.parse(df=dftests)

    pnn_para['feature_size'] = fd.feature_dim
    pnn_para['field_size'] = len(_xi_train[0])

    y_tests_meta = np.zeros((dftests.shape[0], 1), dtype=float)
    _get = lambda x, l: [x[i1] for i1 in l]
    results_epoch_train = np.zeros((len(folds), pnn_para['epoch']), dtype=float)
    results_epoch_valid = np.zeros((len(folds), pnn_para['epoch']), dtype=float)

    # 遍历K折分层采样的train data
    for i, (train_idx, valid_idx) in enumerate(folds):
        # 计算每次使用的train/valid
        xi_train = _get(_xi_train, train_idx)
        xv_train = _get(_xv_train, train_idx)
        y_train_ = _get(_y_train, train_idx)
        xi_valid = _get(_xi_train, valid_idx)
        xv_valid = _get(_xv_train, valid_idx)
        y_valid_ = _get(_y_train, valid_idx)

        # 模型建立/训练/预测
        pnn = ProductNN(**pnn_para)
        pnn.fit(xi_train, xv_train, y_train_, xi_valid, xv_valid, y_valid_)

        y_tests_meta += pnn.predict(_xi_tests, _xv_tests)
        results_epoch_train[i] = pnn.train_result
        results_epoch_valid[i] = pnn.valid_result

    y_tests_meta /= float(len(folds))

    # save result
    clf_str = "ProductNN"
    print("%s: %d Epoch" % (clf_str, pnn_para['epoch']))
    filename = "%s_%dEpoch.csv" % (clf_str, pnn_para['epoch'])
    _make_submission(_id_test, y_tests_meta, filename)

    _plot_fig(results_epoch_train, results_epoch_valid, clf_str)

    return y_tests_meta


def _make_submission(ids, y_pred, filename="submission.csv"):
    pd.DataFrame({"id": ids, "target": y_pred.flatten()}).to_csv(
        os.path.join(config.SUB_DIR, filename), index=False, float_format="%.5f")


def _plot_fig(train_results, valid_results, model_name):
    colors = ["red", "blue", "green"]
    xs = np.arange(1, train_results.shape[1]+1)
    plt.figure()
    legends = []
    for i in range(train_results.shape[0]):
        plt.plot(xs, train_results[i], color=colors[i], linestyle="solid", marker="o")
        plt.plot(xs, valid_results[i], color=colors[i], linestyle="dashed", marker="o")
        legends.append("train-%d" % (i+1))
        legends.append("valid-%d" % (i+1))
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.title("%s" % model_name)
    plt.legend(legends)
    plt.savefig("fig/%s.png" % model_name)
    plt.close()


# =============== ProductNN模型参数设置 =============== #
pnn_params = {
    "embedding_size": 8,
    "deep_layers": [32, 32],
    "dropout_deep": [0.5, 0.5, 0.5],
    "deep_layer_activation": tf.nn.relu,
    "epoch": 30,
    "batch_size": 1024,
    "learning_rate": 0.001,
    "optimizer": "adam",
    "batch_norm": 1,
    "batch_norm_decay": 0.995,
    "verbose": True,
    "deep_init_size": 50,
    "use_inner": False,
    "random_seed": config.RANDOM_SEED
}

print('========== 1.Loading dataset...')
dfTrain, dfTests, x_train, y_train, x_test, ids_test, cat_feature_idx = _load_data()

print('========== 2.Stratifying dataset...')
# 对train data进行分层采样,确保训练集各类别样本比例与原始数据集相同
kfolds = list(StratifiedKFold(n_splits=config.NUM_SPLITS, shuffle=True,
                              random_state=config.RANDOM_SEED).split(x_train, y_train))

print('========== 3.Running base model PNN...')
y_test_dcn = run_base_model_pnn(dfTrain, dfTests, kfolds, pnn_params)

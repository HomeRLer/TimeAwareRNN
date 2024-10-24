import argparse
import os
import pickle
import shutil
import sys
import traceback
from time import time

import numpy as np
import pandas as pd
import torch
from tensorboard_logger import configure, log_value

sys.path.append(os.path.dirname(sys.path[0]))

from taho.model import (
    MIMO,
    GRUCell,
    HOARNNCell,
    HOGRUCell,
    IncrHOARNNCell,
    IncrHOGRUCell,
)
from taho.train import EpochTrainer
from taho.util import SimpleLogger, show_data

GPU = torch.cuda.is_available()


"""
potentially varying input parameters
"""
parser = argparse.ArgumentParser(
    description="Models for Continuous Stirred Tank dataset"
)

# model definition
methods = """
set up model
- model:
    GRU (compensated GRU to avoid linear increase of state; has standard GRU as special case for Euler scheme and equidistant data)
    GRUinc (incremental GRU, for baseline only)
- time_aware:
    no: ignore uneven spacing: for GRU use original GRU implementation; ignore 'scheme' variable
    input: use normalized next interval size as extra input feature
    variable: time-aware implementation
"""


parser.add_argument(
    "--time_aware",
    type=str,
    default="variable",
    choices=["no", "input", "variable"],
    help=methods,
)
parser.add_argument(
    "--model", type=str, default="GRU", choices=["GRU", "GRUinc", "ARNN", "ARNNinc"]
)
parser.add_argument(
    "--interpol", type=str, default="constant", choices=["constant", "linear"]
)

parser.add_argument(
    "--gamma", type=float, default=1.0, help="diffusion parameter ARNN model"
)
parser.add_argument(
    "--step_size",
    type=float,
    default=1.0,
    help="fixed step size parameter in the ARNN model",
)


# data
parser.add_argument(
    "--missing",
    type=float,
    default=0.0,
    help="fraction of missing samples (0.0 or 0.5)",
)

# model architecture
parser.add_argument("--k_state", type=int, default=20, help="dimension of hidden state")

# in case method == 'variable'
RKchoices = ["Euler", "Midpoint", "Kutta3", "RK4"]
parser.add_argument(
    "--scheme",
    type=str,
    default="Euler",
    choices=RKchoices,
    help="Runge-Kutta training scheme",
)

# training
parser.add_argument(
    "--batch_size", type=int, default=16, help="batch size"
)  # Original default value: 512
parser.add_argument(
    "--epochs", type=int, default=1000, help="Number of epochs"
)  # Original default value: 4000
parser.add_argument("--lr", type=float, default=0.001, help="learning rate")
parser.add_argument("--bptt", type=int, default=20, help="bptt")
parser.add_argument("--dropout", type=float, default=0.0, help="drop prob")
parser.add_argument("--l2", type=float, default=0.0, help="L2 regularization")


# admin
parser.add_argument(
    "--save", type=str, default="results", help="experiment logging folder"
)
parser.add_argument(
    "--eval_epochs", type=int, default=20, help="validation every so many epochs"
)
parser.add_argument("--seed", type=int, default=0, help="random seed")

# during development
parser.add_argument(
    "--reset",
    action="store_true",
    help="reset even if same experiment already finished",
)


paras = parser.parse_args()

hard_reset = paras.reset
# if paras.save already exists and contains log.txt:
# reset if not finished, or if hard_reset
log_file = os.path.join(paras.save, "log.txt")
if os.path.isfile(log_file):
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
        completed = "Finished" in content
        if completed and not hard_reset:
            print("Exit; already completed and no hard reset asked.")
            sys.exit()  # do not overwrite folder with current experiment
        else:  # reset folder
            shutil.rmtree(paras.save, ignore_errors=True)


# setup logging
logging = SimpleLogger(log_file)  # log to file
configure(paras.save)  # tensorboard logging
logging("Args: {}".format(paras))


"""
fixed input parameters
"""
frac_dev = 15 / 100
frac_test = 15 / 100

GPU = torch.cuda.is_available()
logging("Using GPU?", GPU)

# set random seed for reproducibility
torch.manual_seed(paras.seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(paras.seed)
np.random.seed(paras.seed)


"""
Load data
"""
## Original version of loading data
# data = np.loadtxt('winding\data\winding_missing_prob_0.00.dat')
# t = np.expand_dims(data[:, 0], axis=1)  # (Nsamples, 1)
# X = data[:, 1:6]  # (Nsamples, 5)
# Y = data[:, 6:8]  # (Nsamples, 2)
# k_in = X.shape[1]
# k_out = Y.shape[1]

# dt = np.expand_dims(data[:, 8], axis=1)  # (Nsamples, 1) # dt: sample rate
# logging('loaded data, \nX', X.shape, '\nY', Y.shape, '\nt', t.shape, '\ndt', dt.shape,
#         '\ntime intervals dt between %.3f and %.3f wide (%.3f on average).'%(np.min(dt), np.max(dt), np.mean(dt)))

# ## HomeRLer's Version of loading data
# data = pd.read_csv("winding\data\odom-19-02-2024-run6.csv", index_col=0).to_numpy()
# t = np.expand_dims(data[:, 0], axis=1)  # (Nsamples, 1)

# X_1 = data[:, 1:11]  # (Nsamples, 10)
# X_2 = data[:, 26:34]  # (Nsamples, 8)
# X = np.hstack((X_1, X_2))  # (Nsamples, 18) x,y,z,qx,qy,qz,qw,bu,bv,bw,pwm1-8
# X = (X - np.min(X, axis=0)) / (np.max(X, axis=0) - np.min(X, axis=0))
# Y = data[:, 11:14]  # (Nsamples, 3)
# k_in = X.shape[1]
# k_out = Y.shape[1]
# sample_rate = 0.1
# dt = sample_rate * np.ones(
#     (X.shape[0], 1)
# )  # (Nsamples, 1) # In out version, assume sample rate is 0.1
# logging(
#     "loaded data, \nX",
#     X.shape,
#     "\nY",
#     Y.shape,
#     "\nt",
#     t.shape,
#     "\ndt",
#     dt.shape,
#     "\ntime intervals dt between %.3f and %.3f wide (%.3f on average)."
#     % (np.min(dt), np.max(dt), np.mean(dt)),
# )
# N = X.shape[0]  # number of samples in total


# ## Load data from the discrete dataset
dataset_dir = "dataset"
files_list = os.listdir(dataset_dir)
file_counts = 0
X: list = []  # (file_nums, sample_nums, feature_nums)
Y: list = []  # (file_nums, sample_nums, feature_nums)
t: list = []  # (file_nums, sample_nums, 1)
sample_num_list: list = []  # (file_nums, 1)
for file in files_list:
    file_dir = dataset_dir + "/" + file
    data = pd.read_csv(file_dir).to_numpy()
    file_counts += 1
    X.append(data[:, 1:11])  # (sample_nums, 10)
    Y.append(data[:, 11:14])  # (sample_nums, 3)
    t.append(data[:, 0])  # (sample_nums, 1)
    sample_num_list.append(data.shape[0])  # (file_nums, 1)

N = sum([subdataset_X.shape[0] for subdataset_X in X])
sample_rate = 0.1
sample_num_list: np.ndarray = np.array(sample_num_list)
dt = sample_rate * np.ones((N, 1))  # (total_sample_nums, 1)
k_in = X[0].shape[1]
k_out = Y[0].shape[1]
logging(
    "Data has loaded, \nloaded subdatasets numbers,",
    file_counts,
    "\nX feature numbers:",
    k_in,
    "\nY feature numbers:",
    k_out,
    "\ntotal sample numbers:",
    N,
    "\nsample rate:",
    sample_rate,
    "\nsample nums of different csvs:",
    sample_num_list,
)


Ndev_num_list = frac_dev * sample_num_list
Ndev_num_list = Ndev_num_list.astype(int).tolist()
Ntest_num_list = frac_test * sample_num_list
Ntest_num_list = Ntest_num_list.astype(int).tolist()
Ntrain_num_list = sample_num_list - Ndev_num_list - Ntest_num_list

logging(
    "Totally, there are {} samples for training, then {} samples for development and {} samples for testing".format(
        np.sum(Ntrain_num_list), np.sum(Ndev_num_list), np.sum(Ntest_num_list)
    )
)


# Ndev = int(frac_dev * N)
# Ntest = int(frac_test * N)
# Ntrain = N - Ntest - Ndev

# logging(
#     "first {} for training, then {} for development and {} for testing".format(
#         Ntrain, Ndev, Ntest
#     )
# )

"""
evaluation function
RRSE error
"""


def prediction_error(truth: np.ndarray, prediction: np.ndarray):
    assert (
        truth.shape == prediction.shape
    ), "Incompatible truth and prediction for calculating prediction error"
    # each shape (sequence, n_outputs)
    # Root Relative Squared Error
    se = np.sum((truth - prediction) ** 2, axis=0)  # summed squared error per channel
    rse = se / np.sum((truth - np.mean(truth, axis=0)) ** 2)  # relative squared error
    rrse = np.mean(np.sqrt(rse))  # square root, followed by mean over channels
    return 100 * rrse  # in percentage


Xtrain: list = []
Ytrain: list = []
ttrain: list = []
dttrain: list = []

Xdev: list = []
Ydev: list = []
tdev: list = []
dtdev: list = []

Xtest: list = []
Ytest: list = []
ttest: list = []
dttest: list = []

for (i, Ndev), Ntrain in zip(enumerate(Ndev_num_list), Ntrain_num_list):
    Xtrain.append(X[i][:Ntrain, :])
    Ytrain.append(Y[i][1 : Ntrain + 1, :])
    ttrain.append(t[i][1 : Ntrain + 1])
    dttrain.append(dt[:Ntrain])

    Xdev.append(X[i][Ntrain : Ntrain + Ndev, :])
    Ydev.append(Y[i][Ntrain + 1 : Ntrain + Ndev + 1, :])
    tdev.append(t[i][Ntrain + 1 : Ntrain + Ndev + 1])
    dtdev.append(dt[Ntrain : Ntrain + Ndev])

    Xtest.append(X[i][Ntrain + Ndev : -1, :])
    Ytest.append(Y[i][Ntrain + Ndev + 1 :, :])
    ttest.append(t[i][Ntrain + Ndev + 1 :])
    dttest.append(dt[Ntrain + Ndev : -1])

# Xtrain = X[:Ntrain, :]
# dttrain = dt[:Ntrain, :]
# Ytrain = Y[1 : Ntrain + 1, :]
# ttrain = t[1 : Ntrain + 1, :]

# Xdev = X[Ntrain : Ntrain + Ndev, :]
# dtdev = dt[Ntrain : Ntrain + Ndev, :]
# Ydev = Y[Ntrain + 1 : Ntrain + Ndev + 1, :]
# tdev = t[Ntrain + 1 : Ntrain + Ndev + 1, :]

# Xtest = X[Ntrain + Ndev : -1, :]
# dttest = dt[
#     Ntrain + Ndev : -1, :
# ]  # last value was added artificially in data_processing.py, but is not used.
# Ytest = Y[Ntrain + Ndev + 1 :, :]
# ttest = t[Ntrain + Ndev + 1 :, :]


"""
- model:
    GRU (compensated GRU to avoid linear increase of state; has standard GRU as special case for Euler scheme and equidistant data)
    GRUinc (incremental GRU, for baseline only)
- time_aware:
    no: ignore uneven spacing: for GRU use original GRU implementation
    input: use normalized next interval size as extra input feature
    variable: time-aware implementation
"""

# time_aware options

# if paras.time_aware == "input":
#     # expand X matrices with additional input feature, i.e., normalized duration dt to next sample
#     dt_mean, dt_std = np.mean(dttrain), np.std(dttrain)
#     dttrain_n = (dttrain - dt_mean) / dt_std
#     dtdev_n = (dtdev - dt_mean) / dt_std
#     dttest_n = (dttest - dt_mean) / dt_std

#     Xtrain = np.concatenate([Xtrain, dttrain_n], axis=1)
#     Xdev = np.concatenate([Xdev, dtdev_n], axis=1)
#     Xtest = np.concatenate([Xtest, dttest_n], axis=1)

#     k_in += 1

# if paras.time_aware == "no" or paras.time_aware == "input":
#     # in case 'input': variable intervals already in input X;
#     # now set actual time intervals to 1 (else same effect as time_aware == 'variable')
#     dttrain = np.ones(dttrain.shape)
#     dtdev = np.ones(dtdev.shape)
#     dttest = np.ones(dttest.shape)

# set model:
if paras.model == "GRU":
    cell_factory = GRUCell if paras.time_aware == "no" else HOGRUCell
elif paras.model == "GRUinc":
    cell_factory = IncrHOGRUCell
elif paras.model == "ARNN":
    cell_factory = HOARNNCell
elif paras.model == "ARNNinc":
    cell_factory = IncrHOARNNCell
else:
    raise NotImplementedError("unknown model type " + paras.model)

dt_mean = np.mean(dttrain[0])
model = MIMO(
    k_in,
    k_out,
    paras.k_state,
    dropout=paras.dropout,
    cell_factory=cell_factory,
    meandt=dt_mean,
    train_scheme=paras.scheme,
    eval_scheme=paras.scheme,
    gamma=paras.gamma,
    step_size=paras.step_size,
    interpol=paras.interpol,
)


if GPU:
    model = model.cuda()

params = sum(
    [np.prod(p.size()) for p in model.parameters()]
)  # the total number of parameters
logging(
    "\nModel %s (time_aware: %s, scheme %s) with %d trainable parameters"
    % (paras.model, paras.time_aware, paras.scheme, params)
)
for n, p in model.named_parameters():
    p_params = np.prod(p.size())
    print("\t%s\t%d (cuda: %s)" % (n, p_params, str(p.is_cuda)))

logging("Architecture: ", model)
log_value("model/params", params, 0)

optimizer = torch.optim.Adam(model.parameters(), lr=paras.lr, weight_decay=paras.l2)


# prepare tensors for evaluation
# 都转成行向量
Xtrain_tn = torch.tensor(Xtrain, dtype=torch.float).unsqueeze(0)  # (1, Ntrain, k_in)
Ytrain_tn = torch.tensor(Ytrain, dtype=torch.float).unsqueeze(0)  # (1, Ntrain, k_out)
dttrain_tn = torch.tensor(dttrain, dtype=torch.float).unsqueeze(0)  # (1, Ntrain, 1)
Xdev_tn = torch.tensor(Xdev, dtype=torch.float).unsqueeze(0)  # (1, Ndev, k_in)
Ydev_tn = torch.tensor(Ydev, dtype=torch.float).unsqueeze(0)  # (1, Ndev, k_out)
dtdev_tn = torch.tensor(dtdev, dtype=torch.float).unsqueeze(0)  # (1, Ndev, 1)
Xtest_tn = torch.tensor(Xtest, dtype=torch.float).unsqueeze(0)
Ytest_tn = torch.tensor(Ytest, dtype=torch.float).unsqueeze(0)
dttest_tn = torch.tensor(dttest, dtype=torch.float).unsqueeze(0)

if GPU:
    Xtrain_tn = Xtrain_tn.cuda()
    Ytrain_tn = Ytrain_tn.cuda()
    dttrain_tn = dttrain_tn.cuda()
    Xdev_tn = Xdev_tn.cuda()
    Ydev_tn = Ydev_tn.cuda()
    dtdev_tn = dtdev_tn.cuda()
    Xtest_tn = Xtest_tn.cuda()
    Ytest_tn = Ytest_tn.cuda()
    dttest_tn = dttest_tn.cuda()


def t2np(tensor):
    return tensor.squeeze().detach().cpu().numpy()


trainer = EpochTrainer(
    model,
    optimizer,
    paras.epochs,
    Xtrain,
    Ytrain,
    dttrain,
    batch_size=paras.batch_size,
    gpu=GPU,
    bptt=paras.bptt,
)  # dttrain ignored for all but 'variable' methods

t00 = time()

best_dev_error = 1.0e5
best_dev_epoch = 0
error_test = -1

max_epochs_no_decrease = 1000

try:  # catch error and redirect to logger
    for epoch in range(1, paras.epochs + 1):
        # train 1 epoch
        mse_train = trainer(epoch)

        if epoch % paras.eval_epochs == 0:
            with torch.no_grad():
                model.eval()
                # (1) forecast on train data steps
                Ytrain_pred, htrain_pred = model(Xtrain_tn, dt=dttrain_tn)
                error_train = prediction_error(Ytrain, t2np(Ytrain_pred))

                # (2) forecast on dev data
                Ydev_pred, hdev_pred = model(
                    Xdev_tn, state0=htrain_pred[:, -1, :], dt=dtdev_tn
                )
                mse_dev = model.criterion(Ydev_pred, Ydev_tn).item()
                error_dev = prediction_error(Ydev, t2np(Ydev_pred))

                # report evaluation results
                log_value("train/mse", mse_train, epoch)
                log_value("train/error", error_train, epoch)
                log_value("dev/loss", mse_dev, epoch)
                log_value("dev/error", error_dev, epoch)

                logging(
                    "epoch %04d | loss %.3f (train), %.3f (dev) | error %.3f (train), %.3f (dev) | tt %.2fmin"
                    % (
                        epoch,
                        mse_train,
                        mse_dev,
                        error_train,
                        error_dev,
                        (time() - t00) / 60.0,
                    )
                )
                show_data(
                    ttrain,
                    Ytrain,
                    t2np(Ytrain_pred),
                    paras.save,
                    "current_trainresults",
                    msg="train results (train error %.3f) at iter %d"
                    % (error_train, epoch),
                )
                show_data(
                    tdev,
                    Ydev,
                    t2np(Ydev_pred),
                    paras.save,
                    "current_devresults",
                    msg="dev results (dev error %.3f) at iter %d" % (error_dev, epoch),
                )

                # update best dev model
                if error_dev < best_dev_error:
                    best_dev_error = error_dev
                    best_dev_epoch = epoch
                    log_value("dev/best_error", best_dev_error, epoch)

                    # corresponding test result:
                    Ytest_pred, _ = model(
                        Xtest_tn, state0=hdev_pred[:, -1, :], dt=dttest_tn
                    )
                    error_test = prediction_error(Ytest, t2np(Ytest_pred))
                    log_value("test/corresp_error", error_test, epoch)
                    logging("new best dev error %.3f" % best_dev_error)

                    # make figure of best model on train, dev and test set for debugging
                    show_data(
                        tdev,
                        Ydev,
                        t2np(Ydev_pred),
                        paras.save,
                        "best_dev_devresults",
                        msg="dev results (dev error %.3f) at iter %d"
                        % (error_dev, epoch),
                    )
                    show_data(
                        ttest,
                        Ytest,
                        t2np(Ytest_pred),
                        paras.save,
                        "best_dev_testresults",
                        msg="test results (test error %.3f) at iter %d (=best dev)"
                        % (error_test, epoch),
                    )

                    # save model
                    # torch.save(model.state_dict(), os.path.join(paras.save, 'best_dev_model_state_dict.pt'))
                    torch.save(model, os.path.join(paras.save, "best_dev_model.pt"))

                    # save dev and test predictions of best dev model
                    pickle.dump(
                        {
                            "t_dev": tdev,
                            "y_target_dev": Ydev,
                            "y_pred_dev": t2np(Ydev_pred),
                            "t_test": ttest,
                            "y_target_test": Ytest,
                            "y_pred_test": t2np(Ytest_pred),
                        },
                        open(os.path.join(paras.save, "data4figs.pkl"), "wb"),
                    )

                elif epoch - best_dev_epoch > max_epochs_no_decrease:
                    logging(
                        "Development error did not decrease over %d epochs -- quitting."
                        % max_epochs_no_decrease
                    )
                    break

    log_value("finished/best_dev_error", best_dev_error, 0)
    log_value("finished/corresp_test_error", error_test, 0)

    logging(
        "Finished: best dev error",
        best_dev_error,
        "at epoch",
        best_dev_epoch,
        "with corresp. test error",
        error_test,
    )


except:
    var = traceback.format_exc()
    logging(var)

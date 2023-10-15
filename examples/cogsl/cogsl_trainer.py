import os
# os.environ['CUDA_VISIBLE_DEVICES']='0'
os.environ['TL_BACKEND'] = 'torch'

import sys
sys.path.insert(0, os.path.abspath('../../'))  # adds path2gammagl to execute in command line.

import argparse
import numpy as np
import random
from sklearn.metrics import f1_score
from sklearn.metrics import roc_auc_score


import tensorlayerx as tlx
import tensorlayerx.nn as nn
from gammagl.datasets.cogsl import CoGSLDataset
from gammagl.models.cogsl import CoGSLModel
from gammagl.utils import  mask_to_index, set_device
from tensorlayerx.model import TrainOneStep, WithLoss
from copy import deepcopy

class SemiSpvzLoss(WithLoss):
    def __init__(self, net, loss_fn):
        super(SemiSpvzLoss, self).__init__(backbone=net, loss_fn=loss_fn)

    def forward(self, data, y):
        _, logits = self.backbone_network(data['x'], data['edge_index'])
        train_logits = tlx.gather(logits, data['train_idx'])
        train_y = tlx.gather(data['y'], data['train_idx'])
        loss = self._loss_fn(train_logits, train_y)
        return loss

class VeLoss(WithLoss):
    def __init__(self, net, loss_fn):
        super(VeLoss, self).__init__(backbone=net, loss_fn=loss_fn)
        self.net = net

    def forward(self, data, y):
        new_v1, new_v2 = self.net.get_view(data)
        logits_v1, logits_v2, prob_v1, prob_v2 = self.net.get_cls_loss(new_v1, new_v2, data['x'])
        curr_v = self.net.get_fusion(new_v1, prob_v1, new_v2, prob_v2)
        logits_v = self.net.get_v_cls_loss(curr_v, data['x'])

        views = [curr_v, new_v1, new_v2]

        loss_v1 = self._loss_fn(tlx.gather(logits_v1, data['train_idx']), tlx.gather(data['y'], data['train_idx']))
        loss_v2 = self._loss_fn(tlx.gather(logits_v2, data['train_idx']), tlx.gather(data['y'], data['train_idx']))
        loss_v = self._loss_fn(tlx.gather(logits_v, data['train_idx']), tlx.gather(data['y'], data['train_idx']))

        cls_loss = args.cls_coe * loss_v + (loss_v1 + loss_v2) * (1 - args.cls_coe) / 2

        vv1, vv2, v1v2 = self.net.get_mi_loss(data['x'], views)
        mi_loss = args.mi_coe * v1v2 + (vv1 + vv2) * (1 - args.mi_coe) / 2
        loss = cls_loss - 0.05 * mi_loss
        return loss

class MiLoss(WithLoss):
    def __init__(self, net, loss_fn):
        super(MiLoss, self).__init__(backbone=net, loss_fn=loss_fn)
        self.net = net

    def forward(self, data, y):
        new_v1, new_v2 = self.net.get_view(data)
        logits_v1, logits_v2, prob_v1, prob_v2 = self.net.get_cls_loss(new_v1, new_v2, data['x'])
        curr_v = self.net.get_fusion(new_v1, prob_v1, new_v2, prob_v2)

        views = [curr_v, new_v1, new_v2]

        vv1, vv2, v1v2 = self.net.get_mi_loss(data['x'], views)
        loss = args.mi_coe * v1v2 + (vv1 + vv2) * (1 - args.mi_coe) / 2
        return loss

class ClsLoss(WithLoss):
    def __init__(self, net, loss_fn):
        super(ClsLoss, self).__init__(backbone=net, loss_fn=loss_fn)
        self.net = net

    def forward(self, data, y):
        new_v1, new_v2 = self.net.get_view(data)
        logits_v1, logits_v2, prob_v1, prob_v2 = self.net.get_cls_loss(new_v1, new_v2, data['x'])
        curr_v = self.net.get_fusion(new_v1, prob_v1, new_v2, prob_v2)
        logits_v = self.net.get_v_cls_loss(curr_v, data['x'])

        loss_v1 = self._loss_fn(tlx.gather(logits_v1, data['train_idx']), tlx.gather(data['y'], data['train_idx']))
        loss_v2 = self._loss_fn(tlx.gather(logits_v2, data['train_idx']), tlx.gather(data['y'], data['train_idx']))
        loss_v = self._loss_fn(tlx.gather(logits_v, data['train_idx']), tlx.gather(data['y'], data['train_idx']))
        loss = args.cls_coe * loss_v + (loss_v1 + loss_v2) * (1 - args.cls_coe) / 2
        return loss

def gen_auc_mima(logits, label):
    preds = tlx.argmax(logits, axis=1)
    test_f1_macro = f1_score(label.cpu(), preds.cpu(), average='macro')
    test_f1_micro = f1_score(label.cpu(), preds.cpu(), average='micro')

    best_proba = nn.Softmax(axis=1)(logits)
    if logits.shape[1] != 2:
        auc = roc_auc_score(y_true=label.detach().cpu().numpy(),
                            y_score=best_proba.detach().cpu().numpy(),
                            multi_class='ovr'
                            )
    else:
        auc = roc_auc_score(y_true=label.detach().cpu().numpy(),
                            y_score=best_proba[:, 1].detach().cpu().numpy()
                            )
    return test_f1_macro, test_f1_micro, auc

def accuracy(output, label):
    preds = output.max(1)[1].type_as(label)
    correct = preds.eq(label).double()
    correct = correct.sum()
    return correct / len(label)

def loss_acc(output, y):
    loss = tlx.losses.softmax_cross_entropy_with_logits(output, y)
    acc = accuracy(output, y)
    return loss, acc

def train_mi(main_model, x, views):
    vv1, vv2, v1v2 = main_model.get_mi_loss(x, views)
    loss = args.mi_coe * v1v2 + (vv1 + vv2) * (1 - args.mi_coe) / 2
    return tlx.convert_to_tensor(loss)

def train_cls(main_model, data):
    new_v1, new_v2 = main_model.get_view(data)
    logits_v1, logits_v2, prob_v1, prob_v2 = main_model.get_cls_loss(new_v1, new_v2, data['x'])
    curr_v = main_model.get_fusion(new_v1, prob_v1, new_v2, prob_v2)
    logits_v = main_model.get_v_cls_loss(curr_v, data['x'])

    views = [curr_v, new_v1, new_v2]

    loss_v1, _ = loss_acc(logits_v1[data['train_idx']], data['y'][data['train_idx']])
    loss_v2, _ = loss_acc(logits_v2[data['train_idx']], data['y'][data['train_idx']])
    loss_v, _ = loss_acc(logits_v[data['train_idx']], data['y'][data['train_idx']])
    return args.cls_coe * loss_v + (loss_v1 + loss_v2) * (1 - args.cls_coe) / 2, views

def main(args):
    set_device(int(args.gpu))

    np.random.seed(args.seed)
    random.seed(args.seed)
    tlx.set_seed(args.seed)

    dataset = CoGSLDataset('', args.dataset, args.v1_p, args.v2_p)
    graph = dataset.data
    train_idx = mask_to_index(graph.train_mask)
    test_idx = mask_to_index(graph.test_mask)
    val_idx = mask_to_index(graph.val_mask)
    view1 = tlx.convert_to_tensor(dataset.view1.todense(), dtype=tlx.float32)
    view2 = tlx.convert_to_tensor(dataset.view2.todense(), dtype=tlx.float32)

    net = CoGSLModel(dataset.num_node_features, args.cls_hid_1, dataset.num_classes,
                                     args.gen_hid, args.mi_hid_1, args.com_lambda_v1, args.com_lambda_v2,
                           args.lam, args.alpha, args.cls_dropout, args.ve_dropout, args.tau, args.ggl, args.big, args.batch, args.dataset)

    scheduler = tlx.optimizers.lr.ExponentialDecay(learning_rate=args.ve_lr, gamma=0.99)
    opti_ve = tlx.optimizers.Adam(lr=scheduler, weight_decay=args.ve_weight_decay)
    opti_cls = tlx.optimizers.Adam(lr=args.cls_lr, weight_decay=args.cls_weight_decay)
    opti_mi = tlx.optimizers.Adam(lr=args.mi_lr, weight_decay=args.mi_weight_decay)

    ve_train_weights = net.ve.trainable_weights
    cls_train_weights = net.cls.trainable_weights
    mi_train_weights = net.mi.trainable_weights

    ve_loss = VeLoss(net, tlx.losses.softmax_cross_entropy_with_logits)
    ve_train_one_step = TrainOneStep(ve_loss, opti_ve, ve_train_weights)

    cls_loss = ClsLoss(net, tlx.losses.softmax_cross_entropy_with_logits)
    cls_train_one_step = TrainOneStep(cls_loss, opti_cls, cls_train_weights)

    mi_loss = MiLoss(net, tlx.losses.softmax_cross_entropy_with_logits)
    mi_train_one_step = TrainOneStep(mi_loss, opti_mi, mi_train_weights)
    data = {
        "name" : dataset.name,
        "x": graph.x,
        "y": graph.y,
        "edge_index": graph.edge_index,
        "edge_weight": graph.edge_weight,
        "train_idx": train_idx,
        "test_idx": test_idx,
        "val_idx": val_idx,
        "view1": view1,
        "view2": view2,
        "v1_indice": dataset.v1_indice,
        "v2_indice": dataset.v2_indice,
        "num_nodes": graph.num_nodes,
    }

    best_acc_val = 0
    best_loss_val = 1e9
    best_test = 0
    best_v = None
    best_v_cls_weight = None

    for epoch in range(args.main_epoch):
        # curr = np.log(1 + args.temp_r * epoch)
        # curr = min(max(0.05, curr), 0.1)

        net.set_train()
        for inner_ve in range(args.inner_ve_epoch):
            ve_loss=ve_train_one_step(data, graph.y)
            # print('ve_loss=', ve_loss)

        for inner_cls in range(args.inner_cls_epoch):
            cls_loss=cls_train_one_step(data, graph.y)
            # print('cls_loss=',  cls_loss)

        for inner_mi in range(args.inner_mi_epoch):
            mi_loss=mi_train_one_step(data, graph.y)
            # print('mi_loss=', mi_loss)

        ## validation ##
        net.set_eval()
        _, views = train_cls(net, data)
        logits_v_val = net.get_v_cls_loss(views[0], data['x'])
        loss_val, acc_val = loss_acc(logits_v_val[data['val_idx']], data['y'][data['val_idx']])
        if acc_val >= best_acc_val and best_loss_val > loss_val:
            print("better v!")
            best_acc_val = max(acc_val, best_acc_val)
            best_loss_val = loss_val
            best_v_cls_weight = deepcopy(net.cls.encoder_v.state_dict())
            best_v = views[0]
        print("EPOCH ", epoch, "\tCUR_LOSS_VAL ", loss_val.data.cpu().numpy(), "\tCUR_ACC_Val ",
              acc_val.data.cpu().numpy(), "\tBEST_ACC_VAL ", best_acc_val.data.cpu().numpy())

    ## test ##
    net.cls.encoder_v.load_state_dict(best_v_cls_weight)
    net.set_eval()

    probs = net.cls.encoder_v(data['x'], best_v)
    test_f1_macro, test_f1_micro, auc = gen_auc_mima(probs[data['test_idx']], data['y'][data['test_idx']])
    print("Test_Macro: ", test_f1_macro, "\tTest_Micro: ", test_f1_micro, "\tAUC: ", auc)

    f = open(f'{tlx.BACKEND}_{args.dataset}_'+'results' + ".txt", "a")
    f.write(str(args.seed) + "\t" + "v1_p=" + str(args.v1_p) + "\t" + "v2_p=" + str(args.v2_p) + "\t" + str(test_f1_macro) + "\t" + str(test_f1_micro) + "\t" + str(auc) + "\n")
    f.close()


def wine_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="wine")
    parser.add_argument('--batch', type=int, default=0)

    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--seed', type=int, default=30)

    parser.add_argument('--v1_p', type=str, default="2")
    parser.add_argument('--v2_p', type=str, default="100")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.5)
    parser.add_argument('--com_lambda_v2', type=float, default=0.5)
    parser.add_argument('--gen_hid', type=int, default=64)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.5)
    parser.add_argument('--alpha', type=float, default=0.1)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=128)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.001)
    parser.add_argument('--ve_weight_decay', type=float, default=0.)
    parser.add_argument('--ve_dropout', type=float, default=0.8)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=100)
    parser.add_argument('--inner_ve_epoch', type=int, default=1)
    parser.add_argument('--inner_cls_epoch', type=int, default=1)
    parser.add_argument('--inner_mi_epoch', type=int, default=5)
    parser.add_argument('--temp_r', type=float, default=1e-3)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.5)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args


def breast_cancer_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="breast_cancer")
    parser.add_argument('--batch', type=int, default=0)

    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--seed', type=int, default=68)

    parser.add_argument('--v1_p', type=str, default="1")
    parser.add_argument('--v2_p', type=str, default="300")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.1)
    parser.add_argument('--com_lambda_v2', type=float, default=0.1)
    parser.add_argument('--gen_hid', type=int, default=64)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.9)
    parser.add_argument('--alpha', type=float, default=0.1)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=128)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.01)
    parser.add_argument('--ve_weight_decay', type=float, default=0.)
    parser.add_argument('--ve_dropout', type=float, default=0.5)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=150)
    parser.add_argument('--inner_ve_epoch', type=int, default=1)
    parser.add_argument('--inner_cls_epoch', type=int, default=1)
    parser.add_argument('--inner_mi_epoch', type=int, default=5)
    parser.add_argument('--temp_r', type=float, default=1e-3)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.5)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args


def digits_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="digits")
    parser.add_argument('--batch', type=int, default=0)

    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--seed', type=int, default=2)

    parser.add_argument('--v1_p', type=str, default="1")
    parser.add_argument('--v2_p', type=str, default="100")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.5)
    parser.add_argument('--com_lambda_v2', type=float, default=0.5)
    parser.add_argument('--gen_hid', type=int, default=32)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.5)
    parser.add_argument('--alpha', type=float, default=0.1)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=128)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.01)
    parser.add_argument('--ve_weight_decay', type=float, default=0.)
    parser.add_argument('--ve_dropout', type=float, default=0.5)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=200)
    parser.add_argument('--inner_ve_epoch', type=int, default=1)
    parser.add_argument('--inner_cls_epoch', type=int, default=10)
    parser.add_argument('--inner_mi_epoch', type=int, default=10)
    parser.add_argument('--temp_r', type=float, default=1e-4)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.2)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args


def polblogs_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="polblogs")
    parser.add_argument('--batch', type=int, default=0)

    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--seed', type=int, default=3)

    parser.add_argument('--v1_p', type=str, default="1")
    parser.add_argument('--v2_p', type=str, default="300")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.1)
    parser.add_argument('--com_lambda_v2', type=float, default=1.0)
    parser.add_argument('--gen_hid', type=int, default=64)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.1)
    parser.add_argument('--alpha', type=float, default=0.1)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=128)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.1)
    parser.add_argument('--ve_weight_decay', type=float, default=0.)
    parser.add_argument('--ve_dropout', type=float, default=0.8)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=150)
    parser.add_argument('--inner_ve_epoch', type=int, default=1)
    parser.add_argument('--inner_cls_epoch', type=int, default=5)
    parser.add_argument('--inner_mi_epoch', type=int, default=5)
    parser.add_argument('--temp_r', type=float, default=1e-4)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.8)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args


def citeseer_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="citeseer")
    parser.add_argument('--batch', type=int, default=0)

    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--seed', type=int, default=3)

    parser.add_argument('--v1_p', type=str, default="2")
    parser.add_argument('--v2_p', type=str, default="40")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.1)
    parser.add_argument('--com_lambda_v2', type=float, default=0.1)
    parser.add_argument('--gen_hid', type=int, default=32)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.5)
    parser.add_argument('--alpha', type=float, default=0.1)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=128)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.001)
    parser.add_argument('--ve_weight_decay', type=float, default=0.)
    parser.add_argument('--ve_dropout', type=float, default=0.5)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=200)
    parser.add_argument('--inner_ve_epoch', type=int, default=5)
    parser.add_argument('--inner_cls_epoch', type=int, default=5)
    parser.add_argument('--inner_mi_epoch', type=int, default=10)
    parser.add_argument('--temp_r', type=float, default=1e-4)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.8)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args


def wikics_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="wikics")
    parser.add_argument('--batch', type=int, default=1000)

    parser.add_argument('--gpu', type=int, default=1)
    parser.add_argument('--seed', type=int, default=6)

    parser.add_argument('--v1_p', type=str, default="1")
    parser.add_argument('--v2_p', type=str, default="1")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.5)
    parser.add_argument('--com_lambda_v2', type=float, default=0.5)
    parser.add_argument('--gen_hid', type=int, default=16)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.1)
    parser.add_argument('--alpha', type=float, default=0.1)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=32)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.01)
    parser.add_argument('--ve_weight_decay', type=float, default=0)
    parser.add_argument('--ve_dropout', type=float, default=0.2)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=200)
    parser.add_argument('--inner_ve_epoch', type=int, default=1)
    parser.add_argument('--inner_cls_epoch', type=int, default=1)
    parser.add_argument('--inner_mi_epoch', type=int, default=5)
    parser.add_argument('--temp_r', type=float, default=1e-3)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.5)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args


def ms_params():
    parser = argparse.ArgumentParser()
    #####################################
    ## basic info
    parser.add_argument('--dataset', type=str, default="ms")
    parser.add_argument('--batch', type=int, default=1000)

    parser.add_argument('--gpu', type=int, default=1)
    parser.add_argument('--seed', type=int, default=4)

    parser.add_argument('--v1_p', type=str, default="1")
    parser.add_argument('--v2_p', type=str, default="1")

    parser.add_argument('--cls_hid_1', type=int, default=16)

    ## gen
    parser.add_argument('--com_lambda_v1', type=float, default=0.5)
    parser.add_argument('--com_lambda_v2', type=float, default=0.5)
    parser.add_argument('--gen_hid', type=int, default=32)

    ## fusion
    parser.add_argument('--lam', type=float, default=0.2)
    parser.add_argument('--alpha', type=float, default=1.0)

    ## mi
    parser.add_argument('--mi_hid_1', type=int, default=256)

    ## optimizer
    parser.add_argument('--cls_lr', type=float, default=0.01)
    parser.add_argument('--cls_weight_decay', type=float, default=5e-4)
    parser.add_argument('--cls_dropout', type=float, default=0.5)

    parser.add_argument('--ve_lr', type=float, default=0.0001)
    parser.add_argument('--ve_weight_decay', type=float, default=1e-10)
    parser.add_argument('--ve_dropout', type=float, default=0.8)

    parser.add_argument('--mi_lr', type=float, default=0.01)
    parser.add_argument('--mi_weight_decay', type=float, default=0)

    ## iter
    parser.add_argument('--main_epoch', type=int, default=200)
    parser.add_argument('--inner_ve_epoch', type=int, default=1)
    parser.add_argument('--inner_cls_epoch', type=int, default=15)
    parser.add_argument('--inner_mi_epoch', type=int, default=10)
    parser.add_argument('--temp_r', type=float, default=1e-4)

    ## coe
    parser.add_argument('--cls_coe', type=float, default=0.3)
    parser.add_argument('--mi_coe', type=float, default=0.3)
    parser.add_argument('--tau', type=float, default=0.5)

    ## ptb
    parser.add_argument('--add', action="store_true")
    parser.add_argument('--dele', action="store_true")
    parser.add_argument('--ptb_feat', action='store_true')

    parser.add_argument('--ratio', type=float, default=0.)
    parser.add_argument('--flag', type=int, default=1)
    #####################################

    args, _ = parser.parse_known_args()
    return args

argv = sys.argv
dataset = argv[1].split('=')[1]

def set_params():
    args = wine_params()
    if dataset == "wine":
        args = wine_params()
        args.ggl = False
        args.big = False
    elif dataset == "breast_cancer":
        args = breast_cancer_params()
        args.ggl = False
        args.big = False
    elif dataset == "digits":
        args = digits_params()
        args.ggl = False
        args.big = False
    elif dataset == "polblogs":
        args = polblogs_params()
        args.ggl = False
        args.big = False
    elif dataset == "citeseer":
        args = citeseer_params()
        args.ggl = False
        args.big = False
    elif dataset == "wikics":
        args = wikics_params()
        args.ggl = True
        args.big = True
    elif dataset == "ms":
        args = ms_params()
        args.ggl = True
        args.big = True
    return args


if __name__ == '__main__':
    args = set_params()
    main(args)
import argparse
import os
import torch
import torch.nn.parallel
import torch.utils.data
from collections import defaultdict
from torch.autograd import Variable
from data_utils.ShapeNetDataLoader import ShapeNetDataLoader, load_data
import torch.nn.functional as F
import datetime
import numpy as np
import logging
from pathlib import Path
from utils import test_seg
from tqdm import tqdm
from model.pointnet2 import PointNet2PartSeg
from model.pointnet import PointNetDenseCls, feature_transform_reguliarzer

seg_classes = {'Earphone': [16, 17, 18], 'Motorbike': [30, 31, 32, 33, 34, 35], 'Rocket': [41, 42, 43], 'Car': [8, 9, 10, 11], 'Laptop': [28, 29], 'Cap': [6, 7], 'Skateboard': [44, 45, 46], 'Mug': [36, 37], 'Guitar': [19, 20, 21], 'Bag': [4, 5], 'Lamp': [24, 25, 26, 27], 'Table': [47, 48, 49], 'Airplane': [0, 1, 2, 3], 'Pistol': [38, 39, 40], 'Chair': [12, 13, 14, 15], 'Knife': [22, 23]}
seg_label_to_cat = {} # {0:Airplane, 1:Airplane, ...49:Table}
for cat in seg_classes.keys():
    for label in seg_classes[cat]:
        seg_label_to_cat[label] = cat



def parse_args():
    parser = argparse.ArgumentParser('PointNet2')
    parser.add_argument('--batchsize', type=int, default=32, help='input batch size')
    parser.add_argument('--workers', type=int, default=4, help='number of data loading workers')
    parser.add_argument('--epoch', type=int, default=201, help='number of epochs for training')
    parser.add_argument('--pretrain', type=str, default=None,help='whether use pretrain model')
    parser.add_argument('--gpu', type=str, default='0', help='specify gpu device')
    parser.add_argument('--model_name', type=str, default='pointnet2', help='Name of model')
    parser.add_argument('--learning_rate', type=float, default=0.001, help='learning rate for training')
    parser.add_argument('--decay_rate', type=float, default=1e-4, help='weight decay')
    parser.add_argument('--optimizer', type=str, default='Adam', help='type of optimizer')
    parser.add_argument('--multi_gpu', type=str, default=None, help='whether use multi gpu training')
    parser.add_argument('--feature_transform', default=False, help="use feature transform in pointnet")
    parser.add_argument('--data_augmentation', default=False, help="data augmentation")

    return parser.parse_args()

def main(args):
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu if args.multi_gpu is None else '0,1,2,3'
    '''CREATE DIR'''
    experiment_dir = Path('./experiment/')
    experiment_dir.mkdir(exist_ok=True)
    file_dir = Path(str(experiment_dir) +'/'+ str(datetime.datetime.now().strftime('%Y-%m-%d_%H-%M')))
    file_dir.mkdir(exist_ok=True)
    checkpoints_dir = file_dir.joinpath('checkpoints/')
    checkpoints_dir.mkdir(exist_ok=True)
    log_dir = file_dir.joinpath('logs/')
    log_dir.mkdir(exist_ok=True)

    '''LOG'''
    args = parse_args()
    logger = logging.getLogger(args.model_name)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler(str(log_dir) + '/train_%s_partseg.txt'%args.model_name)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info('---------------------------------------------------TRANING---------------------------------------------------')
    logger.info('PARAMETER ...')
    logger.info(args)
    DATA_PATH = './data/ShapeNet/'
    print('Load data from %s'%DATA_PATH)
    train_data, train_label, test_data, test_label = load_data(DATA_PATH,classification = False)
    print("The shape of training data is: ",train_data.shape)
    logger.info("The number of training data is: %d",train_data.shape[0])
    print("The shape of test data is: ", test_data.shape)
    logger.info("The number of test data is: %d", test_data.shape[0])

    dataset = ShapeNetDataLoader(train_data,train_label,npoints=2048,data_augmentation=args.data_augmentation)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=args.batchsize,
                                             shuffle=True, num_workers=int(args.workers))

    test_dataset = ShapeNetDataLoader(test_data,test_label,npoints=3000,data_augmentation=False,normalize=True)
    testdataloader = torch.utils.data.DataLoader(test_dataset, batch_size=args.batchsize,
                                                 shuffle=True, num_workers=int(args.workers))

    num_classes = 50
    blue = lambda x: '\033[94m' + x + '\033[0m'
    model = PointNet2PartSeg(num_classes) if args.model_name == 'pointnet2'else PointNetDenseCls(num_classes,feature_transform=True)

    if args.pretrain is not None:
        model.load_state_dict(torch.load(args.pretrain))
        print('load model %s'%args.pretrain)
        logger.info('load model %s'%args.pretrain)
    else:
        print('Training from scratch')
        logger.info('Training from scratch')
    pretrain = args.pretrain
    init_epoch = int(pretrain[-14:-11]) if args.pretrain is not None else 0

    def adjust_learning_rate(optimizer, epoch):
        """Sets the learning rate to the initial LR decayed by 30 every 20000 steps"""
        lr = args.learning_rate * (0.5 ** (epoch // 20))
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

    if args.optimizer == 'SGD':
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    elif args.optimizer == 'Adam':
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=args.learning_rate,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=args.decay_rate
        )

    '''GPU selection and multi-GPU'''
    if args.multi_gpu is not None:
        device_ids = [int(x) for x in args.multi_gpu.split(',')]
        torch.backends.cudnn.benchmark = True
        model.cuda(device_ids[0])
        model = torch.nn.DataParallel(model, device_ids=device_ids)
    else:
        model.cuda()

    history = defaultdict(lambda: list())
    best_acc = 0
    best_meaniou = 0

    for epoch in range(init_epoch,args.epoch):
        for i, data in tqdm(enumerate(dataloader, 0),total=len(dataloader),smoothing=0.9):
            points, target = data
            points, target = Variable(points.float()), Variable(target.long())
            points = points.transpose(2, 1)
            points, target = points.cuda(), target.cuda()
            optimizer.zero_grad()
            model = model.train()
            pred, trans_feat = model(points)
            pred = pred.contiguous().view(-1, num_classes)
            target = target.view(-1, 1)[:, 0]
            loss = F.nll_loss(pred, target)
            if args.feature_transform and args.model_name == 'pointnet':
                loss += feature_transform_reguliarzer(trans_feat) * 0.001
            history['loss'].append(loss.cpu().data.numpy())
            loss.backward()
            optimizer.step()
            adjust_learning_rate(optimizer, epoch)

        test_metrics, test_hist_acc, cat_mean_iou = test_seg(model, testdataloader, seg_label_to_cat)

        print('Epoch %d  %s accuracy: %f  meanIOU: %f' % (
                 epoch, blue('test'), test_metrics['accuracy'],test_metrics['iou']))
        print('Learning rate:%f'%optimizer.param_groups[0]['lr'])
        logger.info('Epoch %d  %s accuracy: %f  meanIOU: %f' % (
                 epoch, 'test', test_metrics['accuracy'],test_metrics['iou']))
        if test_metrics['accuracy'] > best_acc:
            best_acc = test_metrics['accuracy']
            torch.save(model.state_dict(), '%s/%s_%.3d_%.4f.pth' % (checkpoints_dir,args.model_name, epoch, best_acc))
            logger.info(cat_mean_iou)
            logger.info('Save model..')
            print('Save model..')
            print(cat_mean_iou)
        if test_metrics['iou'] > best_meaniou:
            best_meaniou = test_metrics['iou']
        print('Best accuracy is: %.5f'%best_acc)
        logger.info('Best accuracy is: %.5f'%best_acc)
        print('Best meanIOU is: %.5f'%best_meaniou)
        logger.info('Best meanIOU is: %.5f'%best_meaniou)


if __name__ == '__main__':
    args = parse_args()
    main(args)


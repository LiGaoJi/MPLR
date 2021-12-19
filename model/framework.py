#!/usr/bin/env python
# -*-coding:utf-8 -*-
# @file    :   framework.py
# @brief   :   Framework for training, evaluating and saving models.
# @author  :   Haotian Li
# @email   :   lcyxlihaotian@126.com

import os
from typing import List, Tuple
from tqdm import tqdm

import numpy as np
import torch
from torch import optim, nn

from dataloader import RTDataLoader
from functional import get_recall
from functional import get_prediction
from functional import in_top_k
from functional import in_top_k_multi


class RTFramework(object):
    def __init__(self,
                 miner: nn.Module,
                 optimizer: optim.Optimizer,
                 dataloader: RTDataLoader,
                 loss_fn=nn.CrossEntropyLoss(),
                 device='cpu',
                 ckpt_file=None,
                 ckpt_save_dir=None
                 ) -> None:
        """
        Args:
            `miner`: an nn.Module instance for logic rule mining
            `optimizer`: an Optimizer instance
            `dataloader`: Training and test data loader
            `loss_fn`: loss function
            `device`: 'cpu' | 'cuda'
            `ckpt_file`: path to saved model checkpoints
            `ckpt_save_dir`: directory to save best model checkpoint
        """
        self.loss_fn = loss_fn
        self.device = device
        self.ckpt_dir = ckpt_save_dir
        self.miner = miner
        self.optimizer = optimizer
        self.dataloader = dataloader
        self.start_epoch = 0
        if ckpt_file:
            self._load_checkpoint(ckpt_file)

    def train(self,
              top_k: int,
              batch_size: int,
              num_sample_batches: int,
              epochs: int = 20,
              valid_freq: int = 1
              ) -> List:
        '''Train `self.miner` on given training data loaded from `self.dataloader`.

        Args:
            `top_k`: for computing Hit@k
            `batch_size`: mini batch size
            `num_sample_batches`: max number of batches for one epoch
            `epochs`: max training epochs in total
            `valid_freq`: `self.miner` will be evaluated on validation dataset
                every `valid_freq` epochs.

        Returns:
            Traning loss and valid accuracies.
        '''
        num_train = len(self.dataloader.train)
        accuracies = []
        losses = []
        best_cnt = 0
        best_acc = 0.

        for epoch in range(self.start_epoch, self.start_epoch + epochs):
            # Training mode.
            self.miner.train()
            print("{:=^100}".format(f"Training epoch {epoch+1}"))
            running_acc = []
            running_recall = []
            running_loss = 0.
            num_trained = 0
            for bid, (qq, hh, tails, mat) in enumerate(
                self.dataloader.one_epoch("train", batch_size, num_sample_batches, True)
            ):
                qq = torch.from_numpy(qq).to(self.device)
                hh = torch.from_numpy(hh).to(self.device)
                TT = torch.zeros(
                    len(tails), self.dataloader.num_entities,
                    dtype=torch.float
                ).to(self.device)
                for i, T in enumerate(tails):
                    TT[i, T] = 1

                logits = self.miner(qq, hh, tails, mat)
                loss = self.loss_fn(logits, TT)
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                bool_top_k = in_top_k_multi(TT.cpu(), logits.cpu(), top_k)
                running_acc += bool_top_k
                recall_list = get_recall(TT.cpu(), logits.cpu())
                running_recall += recall_list
                running_loss += loss.item() * qq.size(0)
                num_trained += qq.size(0)

                if (bid+1) % 1 == 0:
                    acc = np.mean(running_acc)
                    recall = np.mean(running_recall)
                    loss_val = running_loss / num_trained
                    print("loss: {:>7f}\tacc: {:>4.2f}%\trecall: {:>4.2f}%  "
                          "[{:>6}/{:>6d}]".format(
                              loss_val, 100 * acc, 100 * recall,
                              num_trained, num_train
                          ))
            loss = running_loss / num_train
            acc = np.mean(running_acc) * 100
            recall = np.mean(running_recall) * 100
            print("[epoch {}] loss: {:>7f}, "
                  "acc: {:>4.2f}%, recall: {:>4.2f}%".format(
                      epoch+1, loss, acc, recall
                  ))
            losses.append(loss)

            if (epoch + 1) % valid_freq == 0:
                acc = self.eval("valid", batch_size, top_k)
                if acc > best_acc:
                    best_acc = acc
                    best_cnt = 0
                    print(f"Best checkpoint reached at best acc: {(best_acc * 100):.2f}%")
                    self._save_checkpoint("checkpoint", epoch)
                else:
                    best_cnt += 1
            # Early stopping.
            if best_cnt > 2:
                break

        print("\n[Training finished] epochs: {}, best_acc: {:.2f}%".
              format(epoch - self.start_epoch, best_acc))
        return losses, accuracies

    def eval(self,
             dataset_name: str,
             batch_size: int,
             top_k: int,
             prediction_file=None
             ) -> Tuple[float, float]:
        """Evaluate `self.miner` on dataset specified by `dataset_name`.

        Args:
            `dataset_name`: "train" | "valid" | "test"
            `prediction_file`: file path for output prediction results
        Returns:
            Miner accuracy on given samples.
        """
        if prediction_file:
            file_obj = open(prediction_file, 'w')

        accuracies = []
        self.miner.eval()
        # Do not accumulate gradient along propagation chain.
        with torch.no_grad():
            for _, (qq, hh, tt, mat) in enumerate(tqdm(
                self.dataloader.one_epoch(
                    dataset_name, batch_size
                ),
                desc='Predict'
            )):
                qq = torch.from_numpy(qq).to(self.device)
                hh = torch.from_numpy(hh).to(self.device)
                tt = torch.from_numpy(tt)

                logits = self.miner(qq, hh, tt, mat).cpu()
                bool_top_k = in_top_k(tt, logits, top_k)
                accuracies += bool_top_k
                if prediction_file:
                    qq = qq.cpu().numpy()
                    hh = hh.cpu().numpy()
                    tt = tt.numpy()
                    for bid, (q, h, t) in enumerate(zip(qq, hh, tt)):
                        q_str = self.dataloader.id2rel[q]
                        h_str = self.dataloader.id2ent[h]
                        prediction = get_prediction(
                            t, logits[bid].cpu().numpy(),
                            self.dataloader.id2ent
                        )
                        t_str = prediction[-1]
                        to_write = [q_str, h_str, t_str] + prediction
                        file_obj.write(",".join(to_write) + "\n")
        if prediction_file:
            file_obj.close()
        return np.mean(accuracies)

    def _load_checkpoint(self, ckpt: str) -> None:
        '''Load model checkpoint.'''
        if os.path.isfile(ckpt):
            checkpoint = torch.load(ckpt)
            print(f"Successfully loaded checkpoint '{ckpt}'")
            self.miner.load_state_dict(checkpoint['model'])
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.start_epoch = checkpoint['start_epoch']
        else:
            raise Exception(f"No checkpoint found at '{ckpt}'")

    def _save_checkpoint(self,
                         name: str,
                         end_epoch: int
                         ) -> None:
        '''Save model checkpoint.'''
        if not self.ckpt_dir:
            return

        state_dict = {
            'start_epoch': end_epoch,
            'model': self.miner.state_dict(),
            'optimizer': self.optimizer.state_dict(),
        }

        ckpt_file = name + ".pth.tar"
        ckpt_file = os.path.join(self.ckpt_dir, ckpt_file)
        torch.save(state_dict, ckpt_file)

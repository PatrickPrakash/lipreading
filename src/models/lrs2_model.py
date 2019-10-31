import os

import torch
import torchvision.transforms as transforms
from pytorch_trainer import Module, data_loader
from torch import nn, optim
from torch.nn import functional as F
from torch.utils.data import DataLoader

from src.data.ctc_utils import ctc_collate
from src.data.lrs2 import LRS2Dataset
from src.models.ctc_decoder import Decoder
from src.models.resnet import ResNetModel


class LRS2Model(Module):
    def __init__(self, hparams, in_channels=1, augmentations=False):
        super().__init__()
        self.hparams = hparams
        self.in_channels = in_channels
        self.augmentations = augmentations

        self.best_val_wer = 1.0

        self.frontend = nn.Sequential(
            nn.Conv3d(self.in_channels, 64, kernel_size=(5, 7, 7), stride=(1, 2, 2), padding=(2, 3, 3), bias=False),
            nn.BatchNorm3d(64),
            nn.ReLU(True),
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1, 2, 2), padding=(0, 1, 1))
        )
        self.resnet = ResNetModel(layers=hparams.resnet, pretrained=hparams.pretrained)
        self.lstm = nn.LSTM(
            input_size=256,
            hidden_size=256,
            num_layers=2,
            batch_first=True,
            bidirectional=True
        )
        self.fc = nn.Linear(256 * 2, hparams.words)
        self.softmax = nn.LogSoftmax(dim=2)
        self.loss = nn.CTCLoss(reduction='none', zero_infinity=True)
        self.decoder = Decoder(self.train_dataloader.dataset.characters)

        self.epoch = 0

    def forward(self, x):
        x = self.frontend(x)
        x = self.resnet(x)
        x, _ = self.lstm(x)
        x = self.fc(x)
        x = self.softmax(x)
        return x

    def training_step(self, batch):
        frames, y, lengths, y_lengths, idx = batch
        output = self.forward(frames)
        logits = output.transpose(0, 1)
        loss_all = self.loss(F.log_softmax(logits, dim=-1), y, lengths, y_lengths)
        loss = loss_all.mean()

        weight = torch.ones_like(loss_all)
        dlogits = torch.autograd.grad(loss_all, logits, grad_outputs=weight)[0]
        logits.backward(dlogits)

        logs = {'train_loss': loss}
        return {'log': logs}

    def validation_step(self, batch):
        frames, y, lengths, y_lengths, idx = batch

        output = self.forward(frames)
        output = output.transpose(0, 1)

        loss_all = self.loss(F.log_softmax(output, dim=-1), y, lengths, y_lengths)
        loss = loss_all.mean()

        predicted, gt = self.decoder.predict(frames.size(0), output, y, lengths, y_lengths, n_show=5, mode='greedy')

        return {
            'val_loss': loss,
            'predictions': predicted,
            'ground_truth': gt,
        }

    def validation_end(self, outputs):
        predictions = torch.cat([x['predictions'] for x in outputs]).numpy()
        ground_truth = torch.cat([x['ground_truth'] for x in outputs]).numpy()
        wer = self.decoder.wer_batch(predictions, ground_truth)
        cer = self.decoder.cer_batch(predictions, ground_truth)

        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

        if self.best_val_wer < wer:
            self.best_val_wer = wer
        logs = {
            'val_loss': avg_loss,
            'val_cer': cer,
            'val_wer': wer,
            'best_val_acc': self.best_val_wer
        }

        self.epoch += 1
        return {
            'val_loss': avg_loss,
            'val_acc': avg_acc,
            'log': logs,
        }

    def configure_optimizers(self):
        return optim.Adam(self.parameters(), lr=self.hparams.lr, weight_decay=self.hparams.weight_decay)

    @data_loader
    def train_dataloader(self):
        train_data = LRS2Dataset(
            path=self.hparams.data,
            in_channels=self.in_channels,
            augmentations=self.augmentations,
        )
        train_loader = DataLoader(
            train_data,
            shuffle=True,
            batch_size=self.hparams.batch_size, num_workers=self.hparams.workers,
            pin_memory=True,
            collate_fn=ctc_collate,
        )
        return train_loader

    @data_loader
    def val_dataloader(self):
        val_data = LRS2Dataset(
            path=self.hparams.data,
            in_channels=self.in_channels,
            mode='val',
        )
        val_loader = DataLoader(
            val_data, shuffle=False,
            batch_size=self.hparams.batch_size * 2, num_workers=self.hparams.workers,
            collate_fn=ctc_collate,
        )
        return val_loader

    @data_loader
    def test_dataloader(self):
        test_data = LRS2Dataset(
            path=self.hparams.data,
            in_channels=self.in_channels,
            mode='test',
        )
        test_loader = DataLoader(
            test_data, shuffle=False,
            batch_size=self.hparams.batch_size * 2, num_workers=self.hparams.workers,
            collate_fn=ctc_collate,
        )
        return test_loader


def accuracy(output, labels):
    sums = torch.sum(output, dim=1)
    _, predicted = sums.max(dim=1)
    correct = (predicted == labels.squeeze(dim=1)).sum().type(torch.FloatTensor)
    return correct / output.shape[0]

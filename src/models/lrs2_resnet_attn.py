import os
import random
import re

import editdistance
import numpy as np
import torch
import torchvision.transforms as transforms
from pytorch_trainer import Module
from torch import nn, optim
from torch.nn import functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from torch.utils.checkpoint import checkpoint_sequential
from torch.utils.data import DataLoader

from src.data.lrs2 import LRS2Dataset
from src.models.resnet import ResNetModel


class LRS2ResnetAttn(Module):
    def __init__(self, hparams, in_channels=1, pretrain=False):
        super().__init__()
        self.hparams = hparams
        self.in_channels = in_channels
        self.pretrain = pretrain
        self.max_timesteps = 155
        self.max_text_len = 100
        self.pretrain_words = 0
        self.teacher_forcing_ratio = 1.0

        dataset = self.train_dataloader().dataset
        self.int2char = dataset.int2char
        self.char2int = dataset.char2int

        self.frontend = nn.Sequential(
            nn.Conv3d(self.in_channels, 64, kernel_size=(5, 7, 7), stride=(1, 2, 2), padding=(2, 3, 3), bias=False),
            nn.BatchNorm3d(64),
            nn.ReLU(True),
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1, 2, 2), padding=(0, 1, 1))
        )
        self.resnet = ResNetModel(
            layers=hparams.resnet,
            output_dim=512,
            pretrained=True,
            large_input=False
        )
        num_characters = len(dataset.char_list)
        self.spell = Spell(3, 512, num_characters)
        self.device = torch.device("cuda:0")
        self.criterion = nn.CrossEntropyLoss(ignore_index=self.char2int['<pad>'])

        self.lstm = nn.LSTM(
            input_size=512,
            hidden_size=512,
            num_layers=3,
            batch_first=True,
            bidirectional=False,
        )

        self.best_val_cer = 1.0

    def forward(self, x, lengths, target_tensor, enable_teacher=True):
        x = self.frontend(x)
        x = self.resnet(x)
        x = pack_padded_sequence(x, lengths, enforce_sorted=False, batch_first=True)
        x, states = self.lstm(x)
        watch_outputs, _ = pad_packed_sequence(x, batch_first=True)
        spell_hidden = states[0]

        decoder_input = torch.tensor([self.char2int['<sos>']]).repeat(watch_outputs.size(0), 1).to(self.device)
        cell_state = torch.zeros_like(spell_hidden).to(self.device)
        context = torch.zeros(watch_outputs.size(0), 1, spell_hidden.size(2)).to(self.device)

        loss = 0
        results = []
        target_length = target_tensor.size(1)
        for i in range(target_length):
            use_teacher_forcing = True if random.random() < self.teacher_forcing_ratio else False
            decoder_output, spell_hidden, cell_state, context = self.spell(decoder_input, spell_hidden, cell_state, watch_outputs, context)
            _, topi = decoder_output.topk(1, dim=2)
            if enable_teacher and use_teacher_forcing:
                decoder_input = target_tensor[:, i].long().unsqueeze(dim=1)
            else:
                decoder_input = topi.squeeze(dim=1).detach()
            loss += self.criterion(decoder_output.squeeze(dim=1), target_tensor[:, i].long())
            results.append(topi.cpu().squeeze(dim=1))

        results = torch.cat(results, dim=1)
        return loss, results

    def decode(self, results, target_tensor, batch_num, log_interval=1, log=False):
        cer, wer = 0, 0
        target_length = results.size(1)
        batch_size = results.size(0)
        for batch in range(batch_size):
            output = ''
            label = ''
            for index in range(target_length):
                output += self.int2char[int(results[batch, index])]
                label += self.int2char[int(target_tensor[batch, index])]
            label = label.replace('<pad>', ' ').replace('<eos>', '@')
            label = label[:label.find("@")]
            output = output.replace('<eos>', '@').replace('<pad>', '&').replace('<sos>', '&')
            output = output[:output.find('@')].strip()
            output = re.sub(' +', ' ', output)
            if log and batch_num % log_interval == 0:
                print([output, label])
            cer += editdistance.eval(output, label) / max(len(output), len(label))
            output_words, label_words = output.split(" "), label.split(" ")
            wer += editdistance.eval(output_words, label_words) / max(len(output_words), len(label_words))

        return cer / batch_size, wer / batch_size

    def training_step(self, batch, batch_num):
        input_tensor, lengths, target_tensor = batch
        loss, results = self.forward(input_tensor, lengths, target_tensor)
        cer, wer = self.decode(results, target_tensor, batch_num, log_interval=200, log=True)

        logs = {'train_loss': loss, 'train_cer': cer, 'train_wer': wer}
        return {'loss': loss, 'cer': cer, 'log': logs}

    def validation_step(self, batch, batch_num):
        input_tensor, lengths, target_tensor = batch
        loss, results = self.forward(input_tensor, lengths, target_tensor, enable_teacher=False)
        cer, wer = self.decode(results, target_tensor, batch_num, log_interval=10, log=True)

        return {
            'val_loss': loss,
            'val_cer': cer,
            'val_wer': wer,
        }

    def validation_end(self, outputs):
        if self.pretrain:
            print("Skip during pretrain")
            return

        cer = np.mean([x['val_cer'] for x in outputs])
        wer = np.mean([x['val_wer'] for x in outputs])
        loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        if self.best_val_cer > cer:
            self.best_val_cer = cer
        logs = {
            'val_loss': loss,
            'val_cer': cer,
            'val_wer': wer,
            'best_val_cer': self.best_val_cer
        }

        if self.trainer.scheduler is not None:
            self.trainer.scheduler.step(loss)
            for param_group in self.trainer.optimizer.param_groups:
                logs['lr'] = param_group['lr']

        return {
            'val_loss': loss,
            'val_cer': cer,
            'val_wer': wer,
            'log': logs,
        }

    def on_epoch_start(self, epoch):
        pass
        # self.teacher_forcing_ratio = 1.0 - (epoch / self.hparams.epochs)
        # print(f"Use teacher forcing ratio: {self.teacher_forcing_ratio}")

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.hparams.lr, weight_decay=self.hparams.weight_decay)
        # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        #     optimizer,
        #     mode='min',
        #     factor=0.5,
        #     patience=2,
        #     min_lr=1e-6,
        #     verbose=True,
        # )

        return optimizer

    def train_dataloader(self):
        train_data = LRS2Dataset(
            path=self.hparams.data,
            max_text_len=self.max_text_len,
            mode='train',
            max_timesteps=self.max_timesteps,
            pretrain_words=self.pretrain_words,
            pretrain=self.pretrain,
        )
        train_loader = DataLoader(
            train_data,
            shuffle=True,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.workers,
            pin_memory=True,
        )
        return train_loader

    def val_dataloader(self):
        val_data = LRS2Dataset(
            path=self.hparams.data,
            mode='val',
            max_timesteps=self.max_timesteps,
            max_text_len=self.max_text_len,
            pretrain_words=self.pretrain_words,
            pretrain=self.pretrain,
        )
        val_loader = DataLoader(
            val_data, shuffle=False,
            batch_size=self.hparams.batch_size * 2,
            num_workers=self.hparams.workers,
        )
        return val_loader


class Spell(nn.Module):
    def __init__(self, num_layers, hidden_size, output_size):
        super().__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.num_layers = num_layers

        self.embedded = nn.Embedding(self.output_size, self.hidden_size)
        self.lstm = nn.LSTM(self.hidden_size*2, self.hidden_size, self.num_layers, batch_first=True, bidirectional=False)
        self.attention = Attention(hidden_size, hidden_size)
        self.mlp = nn.Sequential(
            nn.Linear(hidden_size*2, hidden_size),
            nn.BatchNorm1d(hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 256),
            nn.BatchNorm1d(256),
            nn.ReLU(),
            nn.Linear(256, output_size)
        )

    def forward(self, input, hidden_state, cell_state, watch_outputs, context):
        input = self.embedded(input)
        concatenated = torch.cat([input, context], dim=2)
        output, (hidden_state, cell_state) = self.lstm(concatenated, (hidden_state, cell_state))
        context = self.attention(hidden_state[-1], watch_outputs)
        output = self.mlp(torch.cat([output, context], dim=2).squeeze(dim=1)).unsqueeze(dim=1)

        return output, hidden_state, cell_state, context


class Attention(nn.Module):
    def __init__(self, hidden_size, annotation_size):
        super().__init__()
        self.dense = nn.Sequential(
            nn.Linear(hidden_size+annotation_size, hidden_size),
            nn.Tanh(),
            nn.Linear(hidden_size, 1)
        )

    def forward(self, prev_hidden_state, annotations):
        batch_size, sequence_length, _ = annotations.size()
        prev_hidden_state = prev_hidden_state.repeat(sequence_length, 1, 1).transpose(0, 1)

        concatenated = torch.cat([prev_hidden_state, annotations], dim=2)
        attn_energies = self.dense(concatenated).squeeze(dim=2)
        alpha = F.softmax(attn_energies, dim=1).unsqueeze(dim=1)
        context = alpha.bmm(annotations)

        return context

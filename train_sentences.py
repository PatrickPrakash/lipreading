import argparse

import psutil
import torch
from pytorch_trainer import (EarlyStopping, ModelCheckpoint, Trainer,
                             WandbLogger)

from src.models.lrs2_resnet_attn import LRS2ResnetAttn
from src.models.lrs2_resnet_ctc import LRS2ResnetCTC
from src.models.wlsnet import WLSNet

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default="data/datasets/lrs2")
    parser.add_argument('--model', default="resnet")
    parser.add_argument('--lm_path')
    parser.add_argument("--checkpoint_dir", type=str, default='data/checkpoints/lrs2')
    parser.add_argument("--checkpoint", type=str)
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight_decay", type=float, default=1e-5)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--resnet", type=int, default=18)
    parser.add_argument("--pretrained", default=True, type=lambda x: (str(x).lower() == 'true'))
    parser.add_argument("--pretrain", default=False, action='store_true')
    parser.add_argument("--use_amp", default=False, action='store_true')
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    args.workers = psutil.cpu_count(logical=False) if args.workers == None else args.workers
    args.pretrained = False if args.checkpoint != None else args.pretrained

    if args.model == 'resnet':
        model = LRS2ResnetAttn(
            hparams=args,
            in_channels=1,
        )
    elif args.model == 'wlsnet':
        model = WLSNet(
            hparams=args,
            in_channels=1,
        )
    else:
        model = LRS2ResnetCTC(
            hparams=args,
            in_channels=1,
            augmentations=False,
        )

    logger = WandbLogger(
        project='lrs2',
        model=model,
    )
    model.logger = logger
    trainer = Trainer(
        seed=args.seed,
        logger=logger,
        gpu_id=0,
        epochs=args.epochs,
        use_amp=args.use_amp,
    )
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Trainable parameters: {trainable_params}")
    logger.log('parameters', trainable_params)
    logger.log_hyperparams(args)

    if args.checkpoint is not None:
        model.pretrain = False
        logs = trainer.validate(model, checkpoint=args.checkpoint)
        logger.log_metrics(logs)
        print(f"Initial metrics: {logs}")

    if args.pretrain:
        train_epochs = args.epochs
        model.pretrain = True
        print("Pretraining model")

        # curriculum with max_sequence_length, max_text_len, number_of_words, epochs
        curriculum = [
            [32, 16, 1, 30],
            [64, 32, 2, 20],
            [96, 40, 3, 20],
            [120, 48, 4, 20],
            [132, 56, 6, 15],
            [148, 64, 8, 10],
            [148, 72, 10, 10],
        ]

        for part in curriculum:
            checkpoint_callback = ModelCheckpoint(
                directory=args.checkpoint_dir,
                period=part[3],
                prefix=f"lrs2_pretrain_{part[2]}",
            )

            trainer.checkpoint_callback = checkpoint_callback
            model.max_timesteps = part[0]
            model.max_text_len = part[1]
            model.pretrain = True
            model.pretrain_words = part[2]
            trainer.epochs = part[3]
            args.epochs = part[3]
            trainer.fit(model)
            logger.save_file(checkpoint_callback.last_checkpoint_path)

        args.epochs = train_epochs
        model.pretrain = False
        trainer.validate(model)
        print("Pretraining finished")

    checkpoint_callback = ModelCheckpoint(
        directory=args.checkpoint_dir,
        save_best_only=True,
        monitor='val_cer',
        mode='min',
        prefix="lrs2",
    )

    trainer.checkpoint_callback = checkpoint_callback
    model.pretrain = False
    model.max_timesteps = 112
    model.max_text_len = 100
    trainer.epochs = args.epochs
    trainer.fit(model)

    logger.save_file(checkpoint_callback.last_checkpoint_path)

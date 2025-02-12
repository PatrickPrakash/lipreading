import glob
import os

import torch
import torchvision
import torchvision.transforms.functional as F
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms

from src.data.transforms import StatefulRandomHorizontalFlip


class OuluVS2Dataset(Dataset):
    def __init__(self, path, mode='train', augmentation=False):
        self.path = path
        self.augmentation = augmentation
        self.max_timesteps = 38
        self.speakers = {
            'train': (0, 40),
            'val': (43, 48),
            'test': (48, 53),
        }[mode]
        self.file_list = self.build_file_list()
        self.transcripts = [
            "Excuse me",
            "Goodbye",
            "Hello",
            "How are you",
            "Nice to meet you",
            "See you",
            "I am sorry",
            "Thank you",
            "Have a good time",
            "You are welcome",
        ]
        self.preprocess()
        print(f'{mode}: samples = {len(self.file_list)}, vocab_size = {len(self.vocab)}')
        print(f'vocab = {"|".join(self.vocab)}')

    def preprocess(self):
        vocab_unordered = {}

        # transcripts_path = os.path.join(self.path, 'transcript_sentence')
        # transcripts = os.listdir(transcripts_path)
        # for transcript in transcripts:
        #     file = os.path.join(transcripts_path, transcript)
        #     for line in open(file, 'r').read().splitlines():
        #         for char in line:
        #             vocab_unordered[char] = True

        for transcript in self.transcripts:
            transcript = transcript.lower()
            for char in transcript:
                vocab_unordered[char] = True
        self.vocab = []
        for char in vocab_unordered:
            self.vocab.append(char)
        self.vocab.sort()
        self.vocab_mapping = {' ': 0}
        for i, char in enumerate(self.vocab):
            self.vocab_mapping[char] = i + 1

    def build_file_list(self):
        videos = []
        video_path = self.path + 'cropped_mouth_mp4_phrase'
        pattern = video_path + "/**/*.mp4"
        files = glob.glob(pattern, recursive=True)
        max_frames = 0
        for file in files:
            split = file.split("/")[-1][:-4].split("_")
            speaker = int(split[0][1:])
            if speaker >= self.speakers[0] and speaker < self.speakers[1]:
                videos.append(file)
        return videos

    def load_utterance(self, speaker, utterance):
        y = []
        # transcript = os.path.join(self.path, 'transcript_sentence', 's' + str(speaker))
        # for line in open(transcript, 'r').read().splitlines():
        #     line.strip('.')
        #     words = line.split(' ')
        #     for char in line:
        #         y.append(self.vocab_mapping[char])

        transcript = self.transcripts[(utterance - 31) // 3].lower()
        for char in transcript:
            y.append(self.vocab_mapping[char])

        return y

    def __len__(self):
        return len(self.file_list)

    def __getitem__(self, idx):
        path = self.file_list[idx]
        x = torch.zeros(3, self.max_timesteps, 100, 120)
        frames, _, _ = torchvision.io.read_video(path)

        if(self.augmentation):
            augmentations = transforms.Compose([
                StatefulRandomHorizontalFlip(0.5),
            ])
        else:
            augmentations = transforms.Compose([])

        transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize((100, 120)),
            augmentations,
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.7136, 0.4906, 0.3283],
                                 std=[0.113855171, 0.107828568, 0.0917060521])
        ])

        for i, frame in enumerate(frames):
            img = transform(frame)
            x[:, i, :, :] = img

        split = path.split("/")[-1][:-4].split("_")
        speaker, view, utterance = [int(x[1:]) for x in split]
        view = [0, 30, 45, 60, 90][view-1]

        y = self.load_utterance(speaker, utterance)
        length = frames.size(0)

        return x, y, length, idx

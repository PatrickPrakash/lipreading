#!/bin/bash

wget http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2
bzip2 -d shape_predictor_68_face_landmarks.dat.bz2
mkdir -p data/dlib
mv shape_predictor_68_face_landmarks.dat data/dlib

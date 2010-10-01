#!/bin/bash

# abort on error
set -o errexit

VIRTUALENVDIR=PYtest

# clean old virtual environment
rm -rf $VIRTUALENVDIR

# build new virutal environment
virtualenv $VIRTUALENVDIR

# activate new virutal environment
source $VIRTUALENVDIR/bin/activate

easy_install pyrex
easy_install cython
easy_install numexpr==1.3
easy_install tables

# Compile and then install into virtual environment
cython flydra/camnode_colors.pyx
python setup.py develop

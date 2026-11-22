#!/bin/bash
# Run the selector natively on a dataset (kit format) and score it. Usage: evaluate.sh DATASET_DIR [extra env...]
set -u
D=$1; shift
OUT=/Users/taran/Desktop/track4/br/build/eval-$(basename $D).jsonl
rm -rf /Users/taran/Desktop/track4/br/build/tmp && mkdir -p /Users/taran/Desktop/track4/br/build/tmp
env "$@" BR_DATA=$D BR_REPO=/Users/taran/Desktop/track4/sqlglot BR_MAP=${BR_MAP_DIR:-/Users/taran/Desktop/track4/br/map} \
  BR_TMP=/Users/taran/Desktop/track4/br/build/tmp PATH=/Users/taran/Desktop/track4/venv/bin:$PATH \
  /usr/bin/time -p /Users/taran/Desktop/track4/venv/bin/python /Users/taran/Desktop/track4/br/selector/main.py > $OUT 2> $OUT.err
tail -4 $OUT.err
python3 /Users/taran/Desktop/track4/kit/score.py $D $D/answers.json $OUT

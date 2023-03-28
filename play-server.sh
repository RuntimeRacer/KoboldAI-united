#!/bin/bash
if [ ! -f "runtime/envs/koboldai/bin/python" ]; then
./install_requirements.sh cuda
fi
bin/micromamba run -r runtime -n koboldai gunicorn --bind :5000 --workers 1 --threads 1 --timeout 0 aiserver:app $*

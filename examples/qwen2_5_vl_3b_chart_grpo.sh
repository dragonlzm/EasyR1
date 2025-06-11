#!/bin/bash

set -x

export PYTHONUNBUFFERED=1

MODEL_PATH=Qwen/Qwen2.5-VL-3B-Instruct  # replace it with your local file path

python3 -m verl.trainer.main \
    config=/home/ec2-user/generated_annotation/all_anno.json \
    data.train_files=? \
    data.val_files=None \
    worker.actor.model.model_path=${MODEL_PATH} \
    trainer.experiment_name=qwen2_5_vl_3b_chart_grpo_testing \
    trainer.n_gpus_per_node=8 \
    data.use_self_dataset=True \
    data.format_prompt=./examples/format_prompt/chart_format.jinja \

/home/ec2-user/updated_code_images
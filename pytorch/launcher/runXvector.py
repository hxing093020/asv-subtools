# -*- coding:utf-8 -*-

# Copyright xmuspeech (Author: Snowdar 2020-02-06)
# Apache 2.0

# This script just support singel-GPU training and it is a simple example of standard x-vector.
# For more, see runSnowdarXvector.py and runResnetXvector.py.

import sys, os
import logging
import argparse
import traceback
import time
import math
import numpy as np

import torch

sys.path.insert(0, 'subtools/pytorch')

import libs.egs.egs as egs
import libs.training.optim as optim
import libs.training.lr_scheduler as learn_rate_scheduler
import libs.training.trainer as trainer
import libs.support.kaldi_common as kaldi_common
import libs.support.utils as utils

# Logger
logger = logging.getLogger('libs')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(pathname)s:%(lineno)s - "
                              "%(funcName)s - %(levelname)s ]\n#### %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# Parser: add this parser to run launcher with some frequent options (really for conveninece).
parser = argparse.ArgumentParser(
        description="""Train xvector framework with pytorch.""",
        formatter_class=argparse.RawTextHelpFormatter,
        conflict_handler='resolve')

parser.add_argument("--stage", type=int, default=3,
                    help="The stage to control the start of training epoch (default 3).\n"
                         "    stage 0: vad-cmn (preprocess_to_egs.sh).\n"
                         "    stage 1: remove utts (preprocess_to_egs.sh).\n"
                         "    stage 2: get chunk egs (preprocess_to_egs.sh).\n"
                         "    stage 3: training.\n"
                         "    stage 4: extract xvector.")

parser.add_argument("--endstage", type=int, default=4,
                    help="The endstage to control the endstart of training epoch (default 4).")

parser.add_argument("--train-stage", type=int, default=-1,
                    help="The stage to control the start of training epoch (default -1).\n"
                         "    -1 -> creating model_dir.\n"
                         "     0 -> model initialization (e.g. transfer learning).\n"
                         "    >0 -> recovering training.")

parser.add_argument("--force-clear", type=str, action=kaldi_common.StrToBoolAction,
                    default=False, choices=["true", "false"],
                    help="Clear the dir generated by preprocess.")

parser.add_argument("--use-gpu", type=str, action=kaldi_common.StrToBoolAction,
                    default=True, choices=["true", "false"],
                    help="Use GPU or not.")

parser.add_argument("--gpu-id", type=str, default="",
                    help="If NULL, then it will be auto-specified.")

parser.add_argument("--benchmark", type=str, action=kaldi_common.StrToBoolAction,
                    default=True, choices=["true", "false"],
                    help="If true, save training time but require a little more gpu-memory.")

parser.add_argument("--run-lr-finder", type=str, action=kaldi_common.StrToBoolAction,
                    default=False, choices=["true", "false"],
                    help="If true, run lr finder rather than training.")

args = parser.parse_args()

##--------------------------------------------------##
## Control options
stage = max(0, args.stage)
endstage = min(4, args.endstage)
train_stage = max(-1, args.train_stage)
##--------------------------------------------------##
## Preprocess options
force_clear=args.force_clear
preprocess_nj = 20
compress=False
cmn = True # traditional cmn process

chunk_size = 200
limit_utts = 8


sample_type="speaker_balance" # sequential | speaker_balance
chunk_num=0 # -1 means using scale, 0 means using max and >0 means itself.
overlap=0.1
scale=1.5 # Get max / num_spks * scale for every speaker.
valid_split_type="--total-spk" # --total-spk or --default
valid_utts = 1024
valid_chunk_num_every_utt = 2
##--------------------------------------------------##
## Training options
use_gpu = args.use_gpu # Default true.
benchmark = args.benchmark # If true, save much training time but require a little more gpu-memory.
gpu_id = args.gpu_id # If NULL, then it will be auto-specified.
run_lr_finder = args.run_lr_finder

egs_params = {
    "aug":None, # None or specaugment
    "aug_params":{"frequency":0.2, "frame":0.2}
}

loader_params = {
    "use_fast_loader":True, # It is a queue loader to prefetch batch and storage.
    "max_prefetch":10,
    "batch_size":512, 
    "shuffle":True, 
    "num_workers":2,
    "pin_memory":False, 
    "drop_last":True,
}

# Difine model_params by model_blueprint w.r.t your model's __init__(model_params).
model_params = {
    "bn_momentum":0.99, 
    "nonlinearity":"relu", 
    "aug_dropout":0.2,  # Should not be too large.
    "training":True, 
    "extracted_embedding":"far" # For extracting. Here it is far or near w.r.t xvector.py.
}

optimizer_params = {
    "name":"ralamb",
    "learn_rate":0.001,
    "beta1":0.9,
    "beta2":0.999,
    "beta3":0.999,
    "weight_decay":1e-1,  # Should be large for decouped weight decay (adamW) and small for L2 regularization (sgd, adam).
    "lookahead.k":5,
    "lookahead.alpha":0. # 0 means not using lookahead and if used, suggest to set it as 0.5.
}

lr_scheduler_params = {
    "name":"warmR",
    "warmR.lr_decay_step":400, # 0 means decay after every epoch and 1 means every iter. 
    "warmR.T_max":6,
    "warmR.T_mult":1,
    "warmR.factor":0.7,  # The max_lr_decay_factor.
    "warmR.eta_min":4e-8,
    "warmR.log_decay":False
}

epochs = 18 # Total epochs to train. It is important. Here 18 = 6 -> 12 -> 18 with warmR.T_mult=1 and warmR.T_max=6.
report_times_every_epoch = None
report_interval_iters = 100 # About validation computation and loss reporting. If report_times_every_epoch is not None, 
                            # then compute report_interval_iters by report_times_every_epoch.
stop_early = False
suffix = "params" # Used in saved model file.
##--------------------------------------------------##
## Other options
exist_model=""  # Use it in transfer learning.
##--------------------------------------------------##
## Main params
traindata="data/mfcc_23_pitch/voxceleb1_train_aug"
egs_dir="exp/egs/mfcc_23_pitch_voxceleb1_train_aug" + "_" + sample_type + "_max"

model_blueprint="subtools/pytorch/model/xvector.py"
model_dir="exp/standard_xv_baseline_warmR_voxceleb1"
##--------------------------------------------------##
##
#### Set seed
utils.set_all_seed(1024) # Note that, in different machine, random still will be different enven with the same seed,
                         # so, you could always get little different results by this launcher comparing to mine.

#### Preprocess
if stage <= 2 and endstage >= 0:
    # Here only give limited options because it is not convenient.
    # Suggest to pre-execute this shell script to make it freedom and then continue to run this launcher.
    kaldi_common.execute_command("sh subtools/pytorch/pipeline/preprocess_to_egs.sh "
                                 "--stage {stage} --endstage {endstage} --valid-split-type {valid_split_type} "
                                 "--nj {nj} --cmn {cmn} --limit-utts {limit_utts} --min-chunk {chunk_size} --overlap {overlap} "
                                 "--sample-type {sample_type} --chunk-num {chunk_num} --scale {scale} --force-clear {force_clear} "
                                 "--valid-num-utts {valid_utts} --valid-chunk-num {valid_chunk_num_every_utt} --compress {compress}"
                                 "{traindata} {egs_dir}".format(stage=stage, endstage=endstage, valid_split_type=valid_split_type, 
                                 nj=preprocess_nj, cmn=str(cmn).lower(), limit_utts=limit_utts, chunk_size=chunk_size, overlap=overlap, 
                                 sample_type=sample_type, chunk_num=chunk_num, scale=scale, force_clear=str(force_clear).lower(), 
                                 valid_utts=valid_utts, valid_chunk_num_every_utt=valid_chunk_num_every_utt, compress=str(compress).lower()
                                 traindata=traindata, egs_dir=egs_dir))


#### Train model
if stage <= 3 <= endstage:
    logger.info("Get model_blueprint from model directory.")
    # Save the raw model_blueprint in model_dir/config and get the copy of model_blueprint path.
    model_blueprint = utils.create_model_dir(model_dir, model_blueprint, stage=train_stage)

    logger.info("Load egs to bunch.")
    # The dict [info] contains feat_dim and num_targets.
    bunch, info = egs.BaseBunch.get_bunch_from_egsdir(egs_dir, egs_params, loader_params)

    logger.info("Create model from model blueprint.")
    # Another way: import the model.py in this python directly, but it is not friendly to the shell script of extracting and
    # I don't want to change anything about extracting script when the model.py is changed.
    model_py = utils.create_model_from_py(model_blueprint)
    # Give your model class name here w.r.t the model.py.
    model = model_py.Xvector(info["feat_dim"], info["num_targets"], **model_params)

    logger.info("Define optimizer and lr_scheduler.")
    optimizer = optim.get_optimizer(model, optimizer_params)
    lr_scheduler = learn_rate_scheduler.LRSchedulerWrapper(optimizer, lr_scheduler_params)

    # Record params to model_dir
    utils.write_list_to_file([egs_params, loader_params, model_params, optimizer_params, 
                              lr_scheduler_params], model_dir+'/config/params.dict')

    logger.info("Init a simple trainer.")
    # Package(Elements:dict, Params:dict}. It is a key parameter's package to trainer and model_dir/config/.
    package = ({"data":bunch, "model":model, "optimizer":optimizer, "lr_scheduler":lr_scheduler},
            {"model_dir":model_dir, "model_blueprint":model_blueprint, "exist_model":exist_model, 
            "start_epoch":train_stage, "epochs":epochs, "use_gpu":use_gpu, "gpu_id":gpu_id, 
            "benchmark":benchmark, "suffix":suffix, "report_times_every_epoch":report_times_every_epoch,
            "report_interval_iters":report_interval_iters, "record_file":"train.csv"})

    trainer = trainer.SimpleTrainer(package)

    if run_lr_finder:
        trainer.run_lr_finder("lr_finder.csv", init_lr=1e-8, final_lr=10., num_iters=2000, beta=0.98)
        endstage = 3 # Do not start extractor.
    else:
        trainer.run()

    # Plan to use del to avoid memeory account after training done and continue to execute stage 4.
    # But it dose not work and is still a problem.
    # Here, give the runLauncher.sh to avoid this problem.
    # del bunch, model, optimizer, lr_scheduler, trainer


#### Extract xvector
if stage <= 4 <= endstage:
    # There are some params for xvector extracting.
    data_root = "data" # It contains all dataset just like Kaldi recipe.
    prefix = "mfcc_23_pitch" # For to_extracted_data.

    to_extracted_positions = ["far", "near"] # Define this w.r.t model_blueprint.
    to_extracted_data = ["voxceleb1_train_aug", "voxceleb1_test"] # All dataset should be in dataroot/prefix.
    to_extracted_epochs = ["6","12","18"] # It is model's name, such as 10.params or final.params (suffix is w.r.t package).

    nj = 10
    force = False
    use_gpu = True
    gpu_id = ""
    sleep_time = 10


    # Run a batch extracting process.
    try:
        for position in to_extracted_positions:
            # Generate the extracting config from nnet config where 
            # which position to extract depends on the 'extracted_embedding' parameter of model_creation (by my design).
            model_blueprint, model_creation = utils.read_nnet_config("{0}/config/nnet.config".format(model_dir))
            model_creation = model_creation.replace("training=True", "training=False") # To save memory without loading some independent components.
            model_creation = model_creation.replace(model_params["extracted_embedding"], position)
            extract_config = "{0}.extract.config".format(position)
            utils.write_nnet_config(model_blueprint, model_creation, "{0}/config/{1}".format(model_dir, extract_config))
            for epoch in to_extracted_epochs:
                model_file = "{0}.{1}".format(epoch, suffix)
                point_name = "{0}_epoch_{1}".format(position, epoch)

                # If run a trainer with background thread (do not be supported now) or run this launcher extrally with stage=4 
                # (it means another process), then this while-listen is useful to start extracting immediately (but require more gpu-memory).
                model_path = "{0}/{1}".format(model_dir, model_file)
                while True:
                    if os.path.exists(model_path):
                        break
                    else:
                        time.sleep(sleep_time)

                for data in to_extracted_data:
                    datadir = "{0}/{1}/{2}".format(data_root, prefix, data)
                    outdir = "{0}/{1}/{2}".format(model_dir, point_name, data)
                    # Use a well-optimized shell script (with multi-processes) to extract xvectors.
                    # Another way: use subtools/splitDataByLength.sh and subtools/pytorch/pipeline/onestep/extract_embeddings.py 
                    # with python's threads to extract xvectors directly, but the shell script is more convenient.
                    kaldi_common.execute_command("sh subtools/pytorch/pipeline/extract_xvectors_for_pytorch.sh "
                                                "--model {model_file} --cmn {cmn} --nj {nj} --use-gpu {use_gpu} --gpu-id '{gpu_id}' "
                                                " --force {force} --nnet-config config/{extract_config} "
                                                "{model_dir} {datadir} {outdir}".format(model_file=model_file, cmn=str(cmn).lower(), nj=nj,
                                                use_gpu=str(use_gpu).lower(), gpu_id=gpu_id, force=str(force).lower(), extract_config=extract_config,
                                                model_dir=model_dir, datadir=datadir, outdir=outdir))
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            traceback.print_exc()
        sys.exit(1) 

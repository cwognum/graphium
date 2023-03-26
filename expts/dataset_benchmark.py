import os
from os.path import dirname, abspath
import yaml
from omegaconf import DictConfig
import timeit
from datetime import datetime
from copy import deepcopy

import goli
from goli.config._loader import load_datamodule
import time
from typing import Optional, List, Sequence
import wandb
import statistics
import tqdm

# Set up the working directory
MAIN_DIR = dirname(dirname(abspath(goli.__file__)))
os.chdir(MAIN_DIR)
CONFIG_FILE = "expts/configs/config_pcqmv2_mpnn.yaml"
# CONFIG_FILE = "expts/configs/config_ipu_qm9.yaml"


def benchmark(fn, *args, message="", log2wandb=False, **kwargs):
    start = time.time()
    value = fn(*args, **kwargs)
    duration = time.time() - start
    print(f"{message} {duration:.3f} secs")
    if log2wandb:
        wandb.log({message: duration})
    return value


def benchmark_dataloader(dataloader, name, n_epochs=5, log2wandb=False):
    print(f"length of {name} dataloader: {len(dataloader)}")
    tputs = [0] * n_epochs
    n_samples = [0] * n_epochs
    n_graphs = [0] * n_epochs
    n_nodes = [0] * n_epochs
    n_edges = [0] * n_epochs
    for i in range(n_epochs):
        start = time.time()
        for data in tqdm.tqdm(dataloader):
            n_samples[i] += 1
            n_graphs[i] += data["features"]["batch"].max().item()
            n_nodes[i] += data["features"]["feat"].shape[-2]
            n_edges[i] += data["features"]["edge_index"].shape[-1]
        tputs[i] = n_samples[i] / (time.time() - start)

    average_tput = statistics.mean(tputs)
    print(f"{name} dataloader average tput {average_tput}")
    print(f"{name} dataloader total samples per epoch {n_samples}")
    print(f"{name} dataloader total graphs per epoch {n_graphs}")
    print(f"{name} dataloader total nodes per epoch {n_nodes}")
    print(f"{name} dataloader total edges per epoch {n_edges}")

    if log2wandb:
        wandb.log({"average tput": average_tput})

        for i in range(n_epochs):
            d = {
                "epoch": i,
                "samples per epoch": n_samples[i],
                "graphs per epoch": n_graphs[i],
                "nodes per epoch": n_nodes[i],
                "edges per epoch": n_edges[i],
            }
            print(d)
            wandb.log(d)


def main(
    cfg: DictConfig,
    stages: Optional[Sequence[str]] = None,
    run_name: str = "dataset_benchmark",
    add_date_time: bool = True,
    log2wandb: bool = False,
) -> None:
    if add_date_time:
        run_name += "_" + datetime.now().strftime("%d.%m.%Y_%H.%M.%S")

    if log2wandb:
        wandb.init(project="multitask-gnn", name=run_name, config=cfg)

    cfg = deepcopy(cfg)

    # Load and initialize the dataset
    datamodule = benchmark(load_datamodule, cfg, message="Load duration", log2wandb=log2wandb)

    benchmark(datamodule.prepare_data, message="Prepare duration", log2wandb=log2wandb)

    if False:  # stages is not None:
        for stage in stages:
            benchmark(datamodule.setup, stage, message=f"Setup {stage} duration", log2wandb=log2wandb)
    else:
        benchmark(datamodule.setup, message=f"Setup duration", log2wandb=log2wandb)

    if stages is None or {"train", "fit"}.intersection(stages):
        dataloader = datamodule.train_dataloader()
        benchmark_dataloader(dataloader, name="train", log2wandb=log2wandb)
    if stages is None or {"val", "valid", "validation"}.intersection(stages):
        dataloader = datamodule.val_dataloader()
        benchmark_dataloader(dataloader, name="validation", log2wandb=log2wandb)
    if stages is None or {"test", "testing"}.intersection(stages):
        dataloader = datamodule.test_dataloader()
        benchmark_dataloader(dataloader, name="testing", log2wandb=log2wandb)


if __name__ == "__main__":
    with open(os.path.join(MAIN_DIR, CONFIG_FILE), "r") as f:
        cfg = yaml.safe_load(f)
    main(cfg, stages=["train"], log2wandb=True)

from typing import Type, List, Dict, Union, Any, Callable, Optional, Tuple, Iterable

import os
import glob
from functools import partial
import importlib.resources
import zipfile
from copy import deepcopy
import time
from tqdm import tqdm
import gc

from loguru import logger
import fsspec
import omegaconf

import pandas as pd
import numpy as np
import datamol as dm
from tqdm import tqdm
import os.path as osp
from fastparquet import ParquetFile

from sklearn.model_selection import train_test_split

import pytorch_lightning as pl
from pytorch_lightning.trainer.states import RunningStage

import torch
from torch.utils.data.dataloader import DataLoader, Dataset
from torch.utils.data import Subset

from goli.utils import fs
from goli.features import (
    mol_to_graph_dict,
    GraphDict,
    mol_to_pyggraph,
)
from goli.data.utils import goli_package_path
from goli.utils.arg_checker import check_arg_iterator
from goli.utils.hashing import get_md5_hash
from goli.data.smiles_transform import (
    did_featurization_fail,
    BatchingSmilesTransform,
    smiles_to_unique_mol_ids,
)
from goli.data.collate import goli_collate_fn
import goli.data.dataset as Datasets
from goli.data.normalization import LabelNormalization

torch.multiprocessing.set_sharing_strategy("file_system")

PCQM4M_meta = {
    "num tasks": 1,
    "eval metric": "mae",
    "download_name": "pcqm4m_kddcup2021",
    "url": "https://dgl-data.s3-accelerate.amazonaws.com/dataset/OGB-LSC/pcqm4m_kddcup2021.zip",  # TODO: Allow PyG
    "data type": "mol",
    "has_node_attr": True,
    "has_edge_attr": True,
    "task type": "regression",
    "num classes": -1,
    "split": "scaffold",
    "additional node files": "None",
    "additional edge files": "None",
    "binary": False,
    "version": 1,
}

PCQM4Mv2_meta = deepcopy(PCQM4M_meta)
PCQM4Mv2_meta.update(
    {
        "download_name": "pcqm4m-v2",
        "url": "https://dgl-data.s3-accelerate.amazonaws.com/dataset/OGB-LSC/pcqm4m-v2.zip",  # TODO: Allow PyG
        "version": 2,
    }
)


class BaseDataModule(pl.LightningDataModule):
    def __init__(
        self,
        batch_size_training: int = 16,
        batch_size_inference: int = 16,
        batch_size_per_pack: Optional[int] = None,
        num_workers: int = 0,
        pin_memory: bool = True,
        persistent_workers: bool = False,
        collate_fn: Optional[Callable] = None,
    ):
        """
        base dataset module for all datasets (to be inherented)

        Parameters:
            batch_size_training: batch size for training
            batch_size_inference: batch size for inference
            num_workers: number of workers for data loading
            pin_memory: whether to pin memory
            persistent_workers: whether to use persistent workers
            collate_fn: collate function for batching
        """
        super().__init__()

        self.batch_size_training = batch_size_training
        self.batch_size_inference = batch_size_inference
        self.batch_size_per_pack = batch_size_per_pack
        if self.batch_size_per_pack is not None:
            # Check that batch_size_per_pack is a divisor of batch_size_training and batch_size_inference
            assert (
                self.batch_size_training % self.batch_size_per_pack == 0
            ), f"batch_size_training must be a multiple of batch_size_per_pack, provided batch_size_training={self.batch_size_training}, batch_size_per_pack={self.batch_size_per_pack}"
            assert (
                self.batch_size_inference % self.batch_size_per_pack == 0
            ), f"batch_size_inference must be a multiple of batch_size_per_pack, provided batch_size_inference={self.batch_size_inference}, batch_size_per_pack={self.batch_size_per_pack}"

        self.num_workers = num_workers
        self.pin_memory = pin_memory
        self.persistent_workers = persistent_workers

        self.collate_fn = self.get_collate_fn(collate_fn)

        self.train_ds = None
        self.val_ds = None
        self.test_ds = None
        self._predict_ds = None

        self._data_is_prepared = False

    def prepare_data(self):
        raise NotImplementedError()

    def setup(self):
        raise NotImplementedError()

    def train_dataloader(self, **kwargs):
        """
        return the training dataloader
        """
        return self.get_dataloader(
            dataset=self.train_ds,  # type: ignore
            shuffle=True,
            stage=RunningStage.TRAINING,
            **kwargs,
        )

    def val_dataloader(self, **kwargs):
        r"""
        return the validation dataloader
        """
        return self.get_dataloader(
            dataset=self.val_ds,  # type: ignore
            shuffle=False,
            stage=RunningStage.VALIDATING,
            **kwargs,
        )

    def test_dataloader(self, **kwargs):
        r"""
        return the test dataloader
        """
        return self.get_dataloader(
            dataset=self.test_ds,  # type: ignore
            shuffle=False,
            stage=RunningStage.TESTING,
            **kwargs,
        )

    def predict_dataloader(self, **kwargs):
        """
        return the dataloader for prediction
        """
        return self.get_dataloader(
            dataset=self.predict_ds,  # type: ignore
            shuffle=False,
            stage=RunningStage.PREDICTING,
            **kwargs,
        )

    def get_collate_fn(self, collate_fn):
        if collate_fn is None:
            # Some values become `inf` when changing data type. `mask_nan` deals with that
            collate_fn = partial(goli_collate_fn, mask_nan=0, batch_size_per_pack=self.batch_size_per_pack)
            collate_fn.__name__ = goli_collate_fn.__name__

        return collate_fn

    @property
    def is_prepared(self):
        raise NotImplementedError()

    @property
    def is_setup(self):
        raise NotImplementedError()

    @property
    def num_node_feats(self):
        raise NotImplementedError()

    @property
    def num_edge_feats(self):
        raise NotImplementedError()

    @property
    def predict_ds(self):
        """Get the dataset used for the prediction"""
        if self._predict_ds is None:
            return self.test_ds
        else:
            return self._predict_ds

    @property
    def get_num_workers(self):
        """
        get the number of workers to use
        """
        if self.num_workers == -1:
            num_workers = os.cpu_count()
            num_workers = num_workers if num_workers is not None else 0
        else:
            num_workers = self.num_workers
        return num_workers

    @predict_ds.setter
    def predict_ds(self, value):
        """Set the dataset for the prediction"""
        self._predict_ds = value

    def get_fake_graph(self):
        raise NotImplementedError()

    # Private methods

    @staticmethod
    def _read_csv(
        path: str,
        **kwargs,
    ) -> pd.DataFrame:
        """
        private method for reading a csv file
        Parameters:
            path: path to the csv file
            kwargs: keyword arguments for pd.read_csv
        Returns:
            pd.DataFrame: the panda dataframe storing molecules
        """

        path = str(path)

        if path.endswith((".csv", ".csv.gz", ".csv.zip", ".csv.bz2")):
            sep = ","
        elif path.endswith((".tsv", ".tsv.gz", ".tsv.zip", ".tsv.bz2")):
            sep = "\t"
        else:
            raise ValueError(f"unsupported file `{path}`")
        kwargs.setdefault("sep", sep)

        if path.startswith("goli://"):
            path = goli_package_path(path)

        df = pd.read_csv(path, **kwargs)
        return df

    @staticmethod
    def _get_data_file_type(path):
        # Extract the extension
        name, ext = os.path.splitext(path)

        # support compressed files
        _, ext2 = os.path.splitext(name)
        if ext2 != "":
            ext = f"{ext2}{ext}"

        if ext.endswith((".parquet")):  # Support parquet files. Compression is implicit
            return "parquet"
        elif ".sdf" in ext:  # support compressed sdf files
            return "sdf"
        elif ".csv" in ext:  # support compressed csv files
            return "csv"
        elif ".tsv" in ext:  # support compressed tsv files
            return "tsv"
        elif ".pkl" in ext:  # support compressed pickle files
            return "pkl"
        elif ext.endswith(".pt"):  # Pytorch tensor files, used for storing index splits
            return "pt"
        else:
            raise ValueError(f"unsupported file `{path}`")

    @staticmethod
    def _get_table_columns(path: str) -> List[str]:
        """
        Get the columns of a table without reading all the data.
        Might be slow to decompress the file if the file is compressed.

        Parameters:
            path: path to the table file

        Returns:
            List[str]: the column names
        """

        datafile_type = BaseDataModule._get_data_file_type(path)

        if datafile_type == "parquet":
            # Read the schema of a parquet file
            file = ParquetFile(path)
            schema = file.pandas_metadata["columns"]
            column_names = [s["name"] for s in schema if s["name"] is not None]
        elif datafile_type == "sdf":
            df = BaseDataModule._read_sdf(path, max_num_mols=5, discard_invalud=True, n_jobs=1)
            column_names = df.columns
        elif datafile_type in ["csv", "tsv"]:
            # Read the schema of a csv / tsv file
            df = BaseDataModule._read_csv(path, nrows=5)
            column_names = df.columns
        return column_names

    @staticmethod
    def _read_parquet(path, **kwargs):
        kwargs.pop("dtype", None)  # Only useful for csv
        column_names = BaseDataModule._get_table_columns(path)

        # Change the 'usecols' parameter to 'columns'
        columns = kwargs.pop("columns", None)
        if "usecols" in kwargs.keys():
            assert columns is None, "Ambiguous value of `columns`"
            columns = kwargs.pop("usecols")
        if columns is None:
            columns = column_names
        for column in columns:
            assert (
                column in column_names
            ), f"Column `{column}` is not in the parquet file with columns {column_names}"

        # Read the parquet file per column, and convert the data to float16 to reduce memory consumption
        all_series = {}
        progress = tqdm(columns)
        for col in progress:
            # Read single column
            progress.set_description(f"Reading parquet column `{col}`")
            this_series = pd.read_parquet(path, columns=[col], engine="fastparquet", **kwargs)[col]

            # Check if the data is float
            first_elem = this_series.iloc[0]
            is_float = False
            if isinstance(first_elem, (list, tuple)):
                is_float = isinstance(first_elem[0], np.floating)
            elif isinstance(first_elem, np.ndarray):
                is_float = isinstance(first_elem, np.floating)

            # Convert floats to float16
            if is_float:
                if isinstance(first_elem, np.ndarray):
                    this_series.update([elem.astype(np.float16) for elem in this_series])
                elif isinstance(first_elem, list):
                    this_series.update([np.asarray(elem).astype(np.float16) for elem in this_series])
                else:
                    this_series = this_series.astype(np.float16)

            all_series[col] = this_series
            gc.collect()  # Reset memory after each column

        # Merge columns into a dataframe
        df = pd.concat(all_series, axis=1)

        return df

    @staticmethod
    def _read_sdf(path: str, mol_col_name: str = "_rdkit_molecule_obj", **kwargs):
        r"""
        read a given sdf file into a pandas dataframe
        uses datamol.read_sdf to read the sdf file
        Parameters:
            path: path to the sdf file
            mol_col_name: name of the column containing the molecule object
            kwargs: arguments to pass to datamol.read_sdf
        Returns:
            pandas dataframe containing the molecules and their conformer coordinates from the sdf file

        Note: please change mol_col_name to be the column name corresoonding to the molecule object in your sdf file
        """

        # Set default arguments for reading the SDF
        kwargs.setdefault("smiles_column", "smiles")
        kwargs.setdefault("sanitize", False)
        kwargs.setdefault("include_private", True)
        kwargs.setdefault("include_computed", True)
        kwargs.setdefault("remove_hs", True)
        kwargs.setdefault("n_jobs", -1)
        kwargs.setdefault("max_num_mols", kwargs.pop("sample_size", None))
        kwargs.setdefault("discard_invalid", False)

        # Get the interesting columns
        mol_cols = mol_col_name  # adjust this to be the column name in your sdf file
        kwargs.setdefault("mol_column", mol_cols)
        usecols = kwargs.pop("usecols", None)
        dtype = kwargs.pop("dtype", None)
        smiles_col = kwargs["smiles_column"]

        # Read the SDF
        df = dm.read_sdf(path, as_df=True, **kwargs)

        # Keep only the columns needed
        if usecols is not None:
            df = df[usecols + [mol_cols]]

        # Convert the dtypes
        if dtype is not None:
            label_columns = list(set(usecols) - set([mol_cols, smiles_col]))
            dtype_mapper = {col: dtype for col in label_columns}
            df.astype(dtype=dtype_mapper, copy=False)

        return df

    @staticmethod
    def _glob(path: str) -> List[str]:
        """
        glob a given path
        Parameters:
            path: path to glob
        Returns:
            List[str]: list of paths
        """
        files = dm.fs.glob(path)
        files = [f.replace("file://", "") for f in files]
        return files

    def _read_table(self, path: str, **kwargs) -> pd.DataFrame:
        """
        a general read file function which determines if which function to use, either _read_csv or _read_parquet
        Parameters:
            path: path to the file to read
            kwargs: keyword arguments for pd.read_csv or pd.read_parquet
        Returns:
            pd.DataFrame: the panda dataframe storing molecules
        """
        files = self._glob(path)
        if len(files) == 0:
            raise FileNotFoundError("No such file or directory `{path}`")

        if len(files) > 1:
            files = tqdm(sorted(files), desc=f"Reading files at `{path}`")
        dfs = []
        for file in files:
            file_type = self._get_data_file_type(file)
            if file_type == "parquet":
                df = self._read_parquet(file, **kwargs)
            elif file_type == "sdf":  # support compressed sdf files
                df = self._read_sdf(file, **kwargs)
            elif file_type in ["csv", "tsv"]:  # support compressed csv and tsv files
                df = self._read_csv(file, **kwargs)
            else:
                raise ValueError(f"unsupported file `{file}`")
            dfs.append(df)

        return pd.concat(dfs, ignore_index=True)

    def get_dataloader_kwargs(self, stage: RunningStage, shuffle: bool, **kwargs) -> Dict[str, Any]:
        """
        Get the options for the dataloader depending on the current stage.

        Parameters:
            stage: Whether in Training, Validating, Testing, Sanity-checking, Predicting, or Tuning phase.
            shuffle: set to ``True`` to have the data reshuffled at every epoch.

        Returns:
            Arguments to pass to the `DataLoader` during initialization
        """
        loader_kwargs = {}

        # Get batch size and IPU options for training set
        # if stage in [RunningStage.TRAINING, RunningStage.TUNING]:
        if stage in [RunningStage.TRAINING]:
            loader_kwargs["batch_size"] = self.batch_size_training

        # Get batch size and IPU options for validation / testing sets
        elif stage in [RunningStage.VALIDATING, RunningStage.TESTING, RunningStage.PREDICTING]:
            loader_kwargs["batch_size"] = self.batch_size_inference
        else:
            raise ValueError(f"Wrong value for `stage`. Provided `{stage}`")

        # Set default parameters
        loader_kwargs["shuffle"] = shuffle
        loader_kwargs["collate_fn"] = self.collate_fn
        loader_kwargs["num_workers"] = self.get_num_workers
        loader_kwargs["pin_memory"] = self.pin_memory
        loader_kwargs["persistent_workers"] = self.persistent_workers

        # Update from provided parameters
        loader_kwargs.update(**kwargs)

        return loader_kwargs

    def get_dataloader(self, dataset: Dataset, shuffle: bool, stage: RunningStage) -> DataLoader:
        """
        Get the dataloader for a given dataset

        Parameters:
            dataset: The dataset from which to load the data
            shuffle: set to ``True`` to have the data reshuffled at every epoch.
            stage: Whether in Training, Validating, Testing, Sanity-checking, Predicting, or Tuning phase.

        Returns:
            The dataloader to sample from
        """
        kwargs = self.get_dataloader_kwargs(stage=stage, shuffle=shuffle)
        return self._dataloader(dataset=dataset, shuffle=shuffle, stage=stage, **kwargs)

    def _dataloader(self, dataset: Dataset, **kwargs) -> DataLoader:
        r"""
        Get a dataloader for a given dataset
        Parameters:
            dataset: The dataset from which to load the data
            kwargs: keyword arguments for DataLoader
        Returns:
            The dataloader to sample from
        """

        loader = DataLoader(
            dataset=dataset,
            **kwargs,
        )

        return loader

    def get_max_num_nodes_datamodule(self, stages: Optional[List[str]] = None) -> int:
        """
        Get the maximum number of nodes across all datasets from the datamodule

        Parameters:
            datamodule: The datamodule from which to extract the maximum number of nodes
            stages: The stages from which to extract the max num nodes.
                Possible values are ["train", "val", "test", "predict"].
                If None, all stages are considered.

        Returns:
            max_num_nodes: The maximum number of nodes across all datasets from the datamodule
        """

        allowed_stages = ["train", "val", "test", "predict"]
        if stages is None:
            stages = allowed_stages
        for stage in stages:
            assert stage in allowed_stages, f"stage value `{stage}` not allowed."

        max_num_nodes = 0
        # Max number of nodes in the training dataset
        if (self.train_ds is not None) and ("train" in stages):
            max_num_nodes = max(max_num_nodes, self.train_ds.max_num_nodes_per_graph)

        # Max number of nodes in the validation dataset
        if (self.val_ds is not None) and ("val" in stages):
            max_num_nodes = max(max_num_nodes, self.val_ds.max_num_nodes_per_graph)

        # Max number of nodes in the test dataset
        if (self.test_ds is not None) and ("test" in stages):
            max_num_nodes = max(max_num_nodes, self.test_ds.max_num_nodes_per_graph)

        # Max number of nodes in the predict dataset
        if (self.predict_ds is not None) and ("predict" in stages):
            max_num_nodes = max(max_num_nodes, self.predict_ds.max_num_nodes_per_graph)

        return max_num_nodes

    def get_max_num_edges_datamodule(self, stages: Optional[List[str]] = None) -> int:
        """
        Get the maximum number of edges across all datasets from the datamodule

        Parameters:
            datamodule: The datamodule from which to extract the maximum number of nodes
            stages: The stages from which to extract the max num nodes.
                Possible values are ["train", "val", "test", "predict"].
                If None, all stages are considered.

        Returns:
            max_num_edges: The maximum number of edges across all datasets from the datamodule
        """

        allowed_stages = ["train", "val", "test", "predict"]
        if stages is None:
            stages = allowed_stages
        for stage in stages:
            assert stage in allowed_stages, f"stage value `{stage}` not allowed."

        max_num_edges = 0
        # Max number of nodes/edges in the training dataset
        if (self.train_ds is not None) and ("train" in stages):
            max_num_edges = max(max_num_edges, self.train_ds.max_num_edges_per_graph)

        # Max number of nodes/edges in the validation dataset
        if (self.val_ds is not None) and ("val" in stages):
            max_num_edges = max(max_num_edges, self.val_ds.max_num_edges_per_graph)

        # Max number of nodes/edges in the test dataset
        if (self.test_ds is not None) and ("test" in stages):
            max_num_edges = max(max_num_edges, self.test_ds.max_num_edges_per_graph)

        # Max number of nodes/edges in the predict dataset
        if (self.predict_ds is not None) and ("predict" in stages):
            max_num_edges = max(max_num_edges, self.predict_ds.max_num_edges_per_graph)

        return max_num_edges


class DatasetProcessingParams:
    def __init__(
        self,
        df: pd.DataFrame = None,
        df_path: Optional[Union[str, os.PathLike]] = None,
        smiles_col: str = None,
        label_cols: List[str] = None,
        weights_col: str = None,  # Not needed
        weights_type: str = None,  # Not needed
        idx_col: str = None,
        sample_size: Union[int, float, Type[None]] = None,
        split_val: float = 0.2,
        split_test: float = 0.2,
        seed: int = None,
        splits_path: Optional[Union[str, os.PathLike]] = None,
        split_names: Optional[List[str]] = ["train", "val", "test"],
        label_normalization: Optional[Union[Dict[str, Any], omegaconf.DictConfig]] = None,
    ):
        """
        object to store the parameters for the dataset processing
        Parameters:
            df: The dataframe containing the data
            df_path: The path to the dataframe containing the data
            smiles_col: The column name of the smiles
            label_cols: The column names of the labels
            weights_col: The column name of the weights
            weights_type: The type of weights
            idx_col: The column name of the indices
            sample_size: The size of the sample
            split_val: The fraction of the data to use for validation
            split_test: The fraction of the data to use for testing
            seed: The seed to use for the splits and subsampling
            splits_path: The path to the splits
        """
        self.df = df
        self.df_path = df_path
        self.smiles_col = smiles_col
        self.label_cols = label_cols
        self.weights_col = weights_col
        self.weights_type = weights_type
        self.idx_col = idx_col
        self.sample_size = sample_size
        self.split_val = split_val
        self.split_test = split_test
        self.seed = seed
        self.splits_path = splits_path
        self.split_names = split_names
        self.label_normalization = label_normalization


class IPUDataModuleModifier:
    def __init__(
        self,
        ipu_inference_opts: Optional["poptorch.Options"] = None,
        ipu_training_opts: Optional["poptorch.Options"] = None,
        ipu_dataloader_training_opts: Optional["IPUDataloaderOptions"] = None,
        ipu_dataloader_inference_opts: Optional["IPUDataloaderOptions"] = None,
        *args,
        **kwargs,
    ) -> None:
        r"""
        wrapper functions from the a `DataModule` to support IPU and IPU options To be used in dual inheritance, for example:
        ```
        IPUDataModule(BaseDataModule, IPUDataModuleModifier):
            def __init__(self, **kwargs):
                BaseDataModule.__init__(self, **kwargs)
                IPUDataModuleModifier.__init__(self, **kwargs)
        ```

        Parameters:
            ipu_inference_opts: Options for the IPU in inference mode. Ignore if not using IPUs
            ipu_training_opts: Options for the IPU in training mode. Ignore if not using IPUs
            ipu_dataloader_kwargs_train_val: Options for the dataloader for the IPU. Ignore if not using IPUs
            ipu_dataloader_kwargs_test: Options for the dataloader for the IPU. Ignore if not using IPUs
            args: Arguments for the `DataModule`
            kwargs: Keyword arguments for the `DataModule`
        """
        self.ipu_inference_opts = ipu_inference_opts
        self.ipu_training_opts = ipu_training_opts
        self.ipu_dataloader_training_opts = ipu_dataloader_training_opts
        self.ipu_dataloader_inference_opts = ipu_dataloader_inference_opts

    def _dataloader(self, dataset: Dataset, **kwargs) -> "poptorch.DataLoader":
        """
        Get a poptorch dataloader for a given dataset
        Parameters:
            dataset: The dataset to use
            kwargs: Keyword arguments for the dataloader
        Returns:
            The poptorch dataloader
        """

        # Use regular Dataloader if no IPUs
        if ("ipu_options" not in kwargs.keys()) or (kwargs["ipu_options"] is None):
            raise ValueError(f"No IPU options provided.")

        # Initialize the IPU dataloader
        from goli.ipu.ipu_dataloader import create_ipu_dataloader

        loader = create_ipu_dataloader(
            dataset=dataset,
            **kwargs,
        )

        return loader


class MultitaskFromSmilesDataModule(BaseDataModule, IPUDataModuleModifier):
    def __init__(
        self,
        task_specific_args: Dict[str, Any],  # TODO: Replace this with DatasetParams
        cache_data_path: Optional[Union[str, os.PathLike]] = None,
        processed_graph_data_path: Optional[Union[str, os.PathLike]] = None,
        featurization: Optional[Union[Dict[str, Any], omegaconf.DictConfig]] = None,
        batch_size_training: int = 16,
        batch_size_inference: int = 16,
        batch_size_per_pack: Optional[int] = None,
        num_workers: int = 0,
        pin_memory: bool = True,
        persistent_workers: bool = False,
        featurization_n_jobs: int = -1,
        featurization_progress: bool = False,
        featurization_backend: str = "loky",
        featurization_batch_size: int = 1000,
        collate_fn: Optional[Callable] = None,
        prepare_dict_or_graph: str = "pyg:graph",
        **kwargs,
    ):
        """
        only for parameters beginning with task_*, we have a dictionary where the key is the task name
        and the value is specified below.
        Parameters:
            task_df: (value) a dataframe
            task_df_path: (value) a path to a dataframe to load (CSV file). `df` takes precedence over
                `df_path`.
            task_smiles_col: (value) Name of the SMILES column. If set to `None`, it will look for
                a column with the word "smile" (case insensitive) in it.
                If no such column is found, an error will be raised.
            task_label_cols: (value) Name of the columns to use as labels, with different options.

                - `list`: A list of all column names to use
                - `None`: All the columns are used except the SMILES one.
                - `str`: The name of the single column to use
                - `*str`: A string starting by a `*` means all columns whose name
                  ends with the specified `str`
                - `str*`: A string ending by a `*` means all columns whose name
                  starts with the specified `str`

            task_weights_col: (value) Name of the column to use as sample weights. If `None`, no
                weights are used. This parameter cannot be used together with `weights_type`.
            task_weights_type: (value) The type of weights to use. This parameter cannot be used together with `weights_col`.
                **It only supports multi-label binary classification.**

                Supported types:

                - `None`: No weights are used.
                - `"sample_balanced"`: A weight is assigned to each sample inversely
                    proportional to the number of positive value. If there are multiple
                    labels, the product of the weights is used.
                - `"sample_label_balanced"`: Similar to the `"sample_balanced"` weights,
                    but the weights are applied to each element individually, without
                    computing the product of the weights for a given sample.

            task_idx_col: (value) Name of the columns to use as indices. Unused if set to None.
            task_sample_size: (value)

                - `int`: The maximum number of elements to take from the dataset.
                - `float`: Value between 0 and 1 representing the fraction of the dataset to consider
                - `None`: all elements are considered.
            task_split_val: (value) Ratio for the validation split.
            task_split_test: (value) Ratio for the test split.
            task_seed: (value) Seed to use for the random split and subsampling. More complex splitting strategy
                should be implemented.
            task_splits_path: (value) A path a CSV file containing indices for the splits. The file must contains
                3 columns "train", "val" and "test". It takes precedence over `split_val` and `split_test`.

            cache_data_path: path where to save or reload the cached data. The path can be
                remote (S3, GS, etc).
            featurization: args to apply to the SMILES to Graph featurizer.
            batch_size_training: batch size for training and val dataset.
            batch_size_inference: batch size for test dataset.
            num_workers: Number of workers for the dataloader. Use -1 to use all available
                cores.
            pin_memory: Whether to pin on paginated CPU memory for the dataloader.
            featurization_n_jobs: Number of cores to use for the featurization.
            featurization_progress: whether to show a progress bar during featurization.
            featurization_backend: The backend to use for the molecular featurization.

                - "multiprocessing": Found to cause less memory issues.
                - "loky": joblib's Default. Found to cause memory leaks.
                - "threading": Found to be slow.
            featurization_batch_size: Batch size to use for the featurization.

            collate_fn: A custom torch collate function. Default is to `goli.data.goli_collate_fn`
            prepare_dict_or_graph: Whether to preprocess all molecules as Graph dict or PyG graphs.
                Possible options:

                - "pyg:dict": Process molecules as a `dict`. It's faster and requires less RAM during
                  pre-processing. It is slower during training with with `num_workers=0` since
                  pyg `Data` will be created during data-loading, but faster with large
                  `num_workers`, and less likely to cause memory issues with the parallelization.
                - "pyg:graph": Process molecules as `pyg.data.Data`.
        """
        BaseDataModule.__init__(
            self,
            batch_size_training=batch_size_training,
            batch_size_inference=batch_size_inference,
            batch_size_per_pack=batch_size_per_pack,
            num_workers=num_workers,
            pin_memory=pin_memory,
            persistent_workers=persistent_workers,
            collate_fn=collate_fn,
        )
        IPUDataModuleModifier.__init__(self, **kwargs)

        self.task_specific_args = task_specific_args

        # TODO: Have the input argument to the Data Module be of type DatasetParams
        self.task_dataset_processing_params = {
            task: DatasetProcessingParams(**ds_args) for task, ds_args in task_specific_args.items()
        }
        self.featurization_n_jobs = featurization_n_jobs
        self.featurization_progress = featurization_progress
        self.featurization_backend = featurization_backend
        self.featurization_batch_size = featurization_batch_size

        self.task_train_indices = None
        self.task_val_indices = None
        self.task_test_indices = None

        self.single_task_datasets = None
        self.train_singletask_datasets = None
        self.val_singletask_datasets = None
        self.test_singletask_datasets = None

        self.train_ds = None
        self.val_ds = None
        self.test_ds = None

        self.cache_data_path = cache_data_path
        self.processed_graph_data_path = processed_graph_data_path

        self.task_norms = {}

        if featurization is None:
            featurization = {}

        # Whether to transform the smiles into a pyg `Data` graph or a dictionary compatible with pyg
        if prepare_dict_or_graph == "pyg:dict":
            self.smiles_transformer = partial(mol_to_graph_dict, **featurization)
        elif prepare_dict_or_graph == "pyg:graph":
            self.smiles_transformer = partial(mol_to_pyggraph, **featurization)
        else:
            raise ValueError(
                f"`prepare_dict_or_graph` should be either 'pyg:dict' or 'pyg:graph', Provided: `{prepare_dict_or_graph}`"
            )
        self.data_hash = self.get_data_hash()

    def prepare_data(self):
        """Called only from a single process in distributed settings. Steps:

        - If each cache is set and exists, reload from cache and return. Otherwise,
        - For each single-task dataset:
            - Load its dataframe from a path (if provided)
            - Subsample the dataframe
            - Extract the smiles, labels from the dataframe
        - In the previous step, we were also able to get the unique smiles, which we use to compute the features
        - For each single-task dataframe and associated data (smiles, labels, etc.):
            - Filter out the data corresponding to molecules which failed featurization.
            - Create a corresponding SingletaskDataset
            - Split the SingletaskDataset according to the task-specific splits for train, val and test
        """
        if self._data_is_prepared:
            logger.info("Data is already prepared. Skipping the preparation")
            return

        # If a path for data caching is provided, try to load from the path.
        # If successful, skip the data preparation.
        # For next task: load the single graph files for train, val and test data
        cache_data_exists = self.load_data_from_cache()
        # need to check if cache exist properly
        if cache_data_exists:
            self.get_label_statistics(self.processed_graph_data_path, self.data_hash)
            self._data_is_prepared = True
            return

        """Load all single-task dataframes."""
        task_df = {}
        for task, args in self.task_dataset_processing_params.items():
            if args.label_normalization is None:
                args.label_normalization = {}
            label_normalization = LabelNormalization(**args.label_normalization)
            logger.info(f"Reading data for task '{task}'")
            if args.df is None:
                # Only load the useful columns, as some datasets can be very large when loading all columns.
                label_cols = self._parse_label_cols(
                    df=None, df_path=args.df_path, label_cols=args.label_cols, smiles_col=args.smiles_col
                )
                usecols = (
                    check_arg_iterator(args.smiles_col, enforce_type=list)
                    + label_cols
                    + check_arg_iterator(args.idx_col, enforce_type=list)
                    + check_arg_iterator(args.weights_col, enforce_type=list)
                )
                label_dtype = {col: np.float32 for col in label_cols}
                task_df[task] = self._read_table(args.df_path, usecols=usecols, dtype=label_dtype)

            else:
                label_cols = self._parse_label_cols(
                    df=args.df, df_path=None, label_cols=args.label_cols, smiles_col=args.smiles_col
                )
                task_df[task] = args.df
            task_df[task] = task_df[task]
            args.label_cols = label_cols
            self.task_norms[task] = label_normalization
        logger.info("Done reading datasets")

        """Subsample the data frames and extract the necessary data to create SingleTaskDatasets for each task (smiles, labels, extras)."""
        task_dataset_args = {}
        for task in task_df.keys():
            task_dataset_args[task] = {}

        for task, df in task_df.items():
            # Subsample all the dataframes
            sample_size = self.task_dataset_processing_params[task].sample_size
            df = self._sub_sample_df(df, sample_size, self.task_dataset_processing_params[task].seed)

            logger.info(f"Prepare single-task dataset for task '{task}' with {len(df)} data points.")

            # Extract smiles, labels, extras
            args = self.task_dataset_processing_params[task]
            smiles, labels, sample_idx, extras = self._extract_smiles_labels(
                df,
                smiles_col=args.smiles_col,
                label_cols=args.label_cols,
                idx_col=args.idx_col,
                weights_col=args.weights_col,
                weights_type=args.weights_type,
            )

            # Store the relevant information for each task's dataset
            task_dataset_args[task]["smiles"] = smiles
            task_dataset_args[task]["labels"] = labels
            task_dataset_args[task]["sample_idx"] = sample_idx
            task_dataset_args[task]["extras"] = extras

        """Convert SMILES to features (graphs, fingerprints, etc.) for the unique molecules found."""
        all_smiles = []
        all_tasks = []
        idx_per_task = {}
        total_len = 0
        for task, dataset_args in task_dataset_args.items():
            all_smiles.extend(dataset_args["smiles"])
            num_smiles = len(dataset_args["smiles"])
            idx_per_task[task] = (total_len, total_len + num_smiles)
            total_len += num_smiles
            for count in range(len(dataset_args["smiles"])):
                all_tasks.append(task)
        # Get all unique mol ids
        all_mol_ids = smiles_to_unique_mol_ids(
            all_smiles,
            n_jobs=self.featurization_n_jobs,
            featurization_batch_size=self.featurization_batch_size,
            backend=self.featurization_backend,
        )
        unique_mol_ids, unique_idx, inv = np.unique(all_mol_ids, return_index=True, return_inverse=True)

        smiles_to_featurize = [all_smiles[ii] for ii in unique_idx]

        # Convert SMILES to features
        features, _ = self._featurize_molecules(smiles_to_featurize)

        # Store the features (including Nones, which will be filtered in the next step)
        for task in task_dataset_args.keys():
            task_dataset_args[task]["features"] = []
            task_dataset_args[task]["idx_none"] = []
        # Create a list of features matching up with the original smiles
        all_features = [features[unique_idx] for unique_idx in inv]

        # Add the features to the task-specific data
        for all_idx, task in enumerate(all_tasks):
            task_dataset_args[task]["features"].append(all_features[all_idx])

        """Filter data based on molecules which failed featurization. Create single task datasets as well."""
        self.single_task_datasets = {}
        for task, args in task_dataset_args.items():
            # Find out which molecule failed featurization, and filter them out
            idx_none = []
            for idx, feat in enumerate(args["features"]):
                if did_featurization_fail(feat):
                    idx_none.append(idx)
            this_unique_ids = all_mol_ids[idx_per_task[task][0] : idx_per_task[task][1]]
            df, features, smiles, labels, sample_idx, extras, this_unique_ids = self._filter_none_molecules(
                idx_none,
                task_df[task],
                args["features"],
                args["smiles"],
                args["labels"],
                args["sample_idx"],
                args["extras"],
                this_unique_ids,
            )
            task_dataset_args[task]["smiles"] = smiles
            task_dataset_args[task]["labels"] = labels
            task_dataset_args[task]["features"] = features
            task_dataset_args[task]["sample_idx"] = sample_idx
            task_dataset_args[task]["extras"] = extras

            # We have the necessary components to create single-task datasets.
            self.single_task_datasets[task] = Datasets.SingleTaskDataset(
                features=task_dataset_args[task]["features"],
                labels=task_dataset_args[task]["labels"],
                smiles=task_dataset_args[task]["smiles"],
                unique_ids=this_unique_ids,
                **task_dataset_args[task]["extras"],
            )

        """We split the data up to create train, val and test datasets"""
        self.task_train_indices = {}
        self.task_val_indices = {}
        self.task_test_indices = {}

        for task, df in task_df.items():
            train_indices, val_indices, test_indices = self._get_split_indices(
                len(df),
                split_val=self.task_dataset_processing_params[task].split_val,
                split_test=self.task_dataset_processing_params[task].split_test,
                split_seed=self.task_dataset_processing_params[task].seed,
                splits_path=self.task_dataset_processing_params[task].splits_path,
                split_names=self.task_dataset_processing_params[task].split_names,
                sample_idx=task_dataset_args[task]["sample_idx"],
            )
            self.task_train_indices[task] = train_indices
            self.task_val_indices[task] = val_indices
            self.task_test_indices[task] = test_indices

        (
            self.train_singletask_datasets,
            self.val_singletask_datasets,
            self.test_singletask_datasets,
        ) = self.get_subsets_of_datasets(
            self.single_task_datasets, self.task_train_indices, self.task_val_indices, self.task_test_indices
        )

        # When a path is provided but no cache is found, save to cache
        if (self.cache_data_path is not None) and (not cache_data_exists):
            self.save_data_to_cache()

        self._data_is_prepared = True
        # TODO (Gabriela): Implement the ability to save to cache.

    def setup(
        self,
        stage: str = None,
        save_smiles_and_ids: bool = False,
    ):
        """
        Prepare the torch dataset. Called on every GPUs. Setting state here is ok.
        Parameters:
            stage (str): Either 'fit', 'test', or None.
        """

        # Can possibly get rid of setup because a single dataset will have molecules exclusively in train, val or test
        # Produce the label sizes to update the collate function
        labels_size = {}
        if stage == "fit" or stage is None:
            train_load_from_file = False
            processed_train_data_path = None
            val_load_from_file = False
            processed_val_data_path = None
            if self.processed_graph_data_path is not None:
                processed_train_data_path = osp.join(
                    self.processed_graph_data_path, f"train_{self.data_hash}"
                )
                train_load_from_file = (
                    osp.exists(processed_train_data_path)
                    and self.get_folder_size(processed_train_data_path) > 0
                )
                processed_val_data_path = osp.join(self.processed_graph_data_path, f"val_{self.data_hash}")
                val_load_from_file = (
                    osp.exists(processed_val_data_path) and self.get_folder_size(processed_val_data_path) > 0
                )
            self.train_ds = Datasets.MultitaskDataset(
                self.train_singletask_datasets,
                n_jobs=self.featurization_n_jobs,
                backend=self.featurization_backend,
                featurization_batch_size=self.featurization_batch_size,
                progress=self.featurization_progress,
                about="training set",
                save_smiles_and_ids=save_smiles_and_ids,
                data_path=processed_train_data_path,
                load_from_file=train_load_from_file,
            )  # type: ignore
            self.val_ds = Datasets.MultitaskDataset(
                self.val_singletask_datasets,
                n_jobs=self.featurization_n_jobs,
                backend=self.featurization_backend,
                featurization_batch_size=self.featurization_batch_size,
                progress=self.featurization_progress,
                about="validation set",
                save_smiles_and_ids=save_smiles_and_ids,
                data_path=processed_val_data_path,
                load_from_file=val_load_from_file,
            )  # type: ignore
            logger.info(self.train_ds)
            logger.info(self.val_ds)
            if (self.processed_graph_data_path is not None) and (not train_load_from_file):
                self.get_label_statistics(
                    self.processed_graph_data_path, self.data_hash, self.train_ds, train=True
                )
                self.normalize_label(self.train_ds)
                # save featurized train dataset to disk
                self.save_featurized_data(self.train_ds, processed_train_data_path)
            if (self.processed_graph_data_path is not None) and (not val_load_from_file):
                # save featurized validation dataset to disk
                self.save_featurized_data(self.val_ds, processed_val_data_path)

            labels_size.update(
                self.train_ds.labels_size
            )  # Make sure that all task label sizes are contained in here. Maybe do the update outside these if statements.
            labels_size.update(self.val_ds.labels_size)

        if stage == "test" or stage is None:
            test_load_from_file = False
            processed_test_data_path = None
            if self.processed_graph_data_path is not None:
                processed_test_data_path = osp.join(self.processed_graph_data_path, f"test_{self.data_hash}")
                test_load_from_file = (
                    osp.exists(processed_test_data_path)
                    and self.get_folder_size(processed_test_data_path) > 0
                )
            self.test_ds = Datasets.MultitaskDataset(
                self.test_singletask_datasets,
                n_jobs=self.featurization_n_jobs,
                backend=self.featurization_backend,
                featurization_batch_size=self.featurization_batch_size,
                progress=self.featurization_progress,
                about="test set",
                save_smiles_and_ids=save_smiles_and_ids,
                data_path=processed_test_data_path,
                load_from_file=test_load_from_file,
            )  # type: ignore
            logger.info(self.test_ds)
            if (self.processed_graph_data_path is not None) and (not test_load_from_file):
                # save featurized test dataset to disk
                self.save_featurized_data(self.test_ds, processed_test_data_path)

            labels_size.update(self.test_ds.labels_size)

        default_labels_size_dict = self.collate_fn.keywords.get("labels_size_dict", None)

        if default_labels_size_dict is None:
            self.collate_fn.keywords["labels_size_dict"] = labels_size

    def get_folder_size(self, path):
        # check if the data items are actually saved into the folders
        return sum(os.path.getsize(osp.join(path, f)) for f in os.listdir(path))

    def get_label_statistics(self, data_path, data_hash, dataset=None, train=False):
        path_with_hash = os.path.join(data_path, data_hash)
        os.makedirs(path_with_hash, exist_ok=True)
        filename = os.path.join(path_with_hash, "task_norms.pkl")
        # if self.task_norms was obtained from prepare_data step and
        # this is training dataset split and the hash specific task_norms.pkl
        # file does not exist, we recalculate the label statistics.
        if self.task_norms and train and not os.path.isfile(filename):
            for task in dataset[0]["labels"].keys():
                labels = np.stack(np.array([datum["labels"][task] for datum in self.train_ds]), axis=0)
                self.task_norms[task].calculate_statistics(labels)
            torch.save(self.task_norms, filename, pickle_protocol=4)
        # if any of the above three condition does not satisfy, we load from file.
        else:
            self.task_norms = torch.load(filename)

    def normalize_label(self, dataset):
        for task in dataset[0]["labels"].keys():
            for i in range(len(dataset)):
                normalized_label = self.task_norms[task].normalize(dataset[i]["labels"][task])
                dataset[i]["labels"][task] = normalized_label
        return dataset

    def save_featurized_data(self, dataset, processed_data_path):
        for i in range(0, len(dataset), 1000):
            os.makedirs(os.path.join(processed_data_path, format(i // 1000, "04d")), exist_ok=True)
        process_params = [(index, datum, processed_data_path) for index, datum in enumerate(dataset)]

        for param in tqdm(process_params):
            self.process_func(param)
        return

    def process_func(self, param):
        index, datum, folder = param
        filename = os.path.join(folder, format(index // 1000, "04d"), format(index, "07d") + ".pkl")
        torch.save(
            {"graph_with_features": datum["features"], "labels": datum["labels"]}, filename, pickle_protocol=4
        )
        return

    def get_dataloader_kwargs(self, stage: RunningStage, shuffle: bool, **kwargs) -> Dict[str, Any]:
        """
        Get the options for the dataloader depending on the current stage.

        Parameters:
            stage: Whether in Training, Validating, Testing, Sanity-checking, Predicting, or Tuning phase.
            shuffle: set to ``True`` to have the data reshuffled at every epoch.

        Returns:
            Arguments to pass to the `DataLoader` during initialization
        """
        loader_kwargs = super().get_dataloader_kwargs(stage=stage, shuffle=shuffle, **kwargs)

        # Get batch size and IPU options for training set
        # if stage in [RunningStage.TRAINING, RunningStage.TUNING]:
        if stage in [RunningStage.TRAINING]:
            loader_kwargs["ipu_dataloader_options"] = self.ipu_dataloader_training_opts
            loader_kwargs["ipu_options"] = self.ipu_training_opts

        # Get batch size and IPU options for validation / testing sets
        elif stage in [RunningStage.VALIDATING, RunningStage.TESTING, RunningStage.PREDICTING]:
            loader_kwargs["ipu_dataloader_options"] = self.ipu_dataloader_inference_opts
            loader_kwargs["ipu_options"] = self.ipu_inference_opts
        else:
            raise ValueError(f"Wrong value for `stage`. Provided `{stage}`")

        # Remove the IPU options if not available
        if loader_kwargs["ipu_options"] is None:
            loader_kwargs.pop("ipu_options")
            if loader_kwargs["ipu_dataloader_options"] is not None:
                logger.warning(
                    "`ipu_dataloader_options` will be ignored since it is provided without `ipu_options`."
                )
            loader_kwargs.pop("ipu_dataloader_options")
        return loader_kwargs

    def get_dataloader(
        self, dataset: Dataset, shuffle: bool, stage: RunningStage
    ) -> Union[DataLoader, "poptorch.DataLoader"]:
        """
        Get the poptorch dataloader for a given dataset

        Parameters:
            dataset: The dataset from which to load the data
            shuffle: set to ``True`` to have the data reshuffled at every epoch.
            stage: Whether in Training, Validating, Testing, Sanity-checking, Predicting, or Tuning phase.

        Returns:
            The poptorch dataloader to sample from
        """
        kwargs = self.get_dataloader_kwargs(stage=stage, shuffle=shuffle)
        is_ipu = ("ipu_options" in kwargs.keys()) and (kwargs.get("ipu_options") is not None)
        if is_ipu:
            loader = IPUDataModuleModifier._dataloader(self, dataset=dataset, **kwargs)
        else:
            loader = BaseDataModule._dataloader(self, dataset=dataset, **kwargs)

        return loader

    def get_collate_fn(self, collate_fn):
        if collate_fn is None:
            # Some values become `inf` when changing data type. `mask_nan` deals with that
            collate_fn = partial(
                goli_collate_fn,
                mask_nan=0,
                do_not_collate_keys=["smiles", "mol_ids"],
                batch_size_per_pack=self.batch_size_per_pack,
            )
            collate_fn.__name__ = goli_collate_fn.__name__
        return collate_fn

    # Cannot be used as is for the multitask version, because sample_idx does not apply.
    def _featurize_molecules(self, smiles: Iterable[str]) -> Tuple[List, List]:
        """
        Precompute the features (graphs, fingerprints, etc.) from the SMILES.
        Features are computed from `self.smiles_transformer`.
        A warning is issued to mention which molecules failed featurization.

        Note:
            (hadim): in case of very large dataset we could:
            - or cache the data and read from it during `next(iter(dataloader))`
            - or compute the features on-the-fly during `next(iter(dataloader))`
            For now we compute in advance and hold everything in memory.

        Parameters:
            smiles: A list of all the molecular SMILES to featurize
            sample_idx: The indexes corresponding to the sampled SMILES.
                If not provided, computed from `numpy.arange`.

        Returns:
            features: A list of all the featurized molecules
            idx_none: A list of the indexes that failed featurization
        """

        batch_size = BatchingSmilesTransform.parse_batch_size(
            numel=len(smiles),
            desired_batch_size=self.featurization_batch_size,
            n_jobs=self.featurization_n_jobs,
        )

        # Loop all the smiles and compute the features
        features = dm.parallelized_with_batches(
            BatchingSmilesTransform(self.smiles_transformer),
            smiles,
            batch_size=batch_size,
            progress=True,
            n_jobs=self.featurization_n_jobs,
            backend=self.featurization_backend,
            tqdm_kwargs={"desc": f"featurizing_smiles, batch={batch_size}"},
        )

        # Warn about None molecules
        idx_none = [ii for ii, feat in enumerate(features) if did_featurization_fail(feat)]
        if len(idx_none) > 0:
            mols_to_msg = [
                f"idx={idx} - smiles={smiles[idx]} - Error_msg[:-200]=\n{str(features[idx])[:-200]}"
                for idx in idx_none
            ]
            msg = "\n".join(mols_to_msg)
            logger.warning(
                (f"{len(idx_none)} molecules will be removed since they failed featurization:\n" + msg)
            )

        return features, idx_none

    @staticmethod
    def _filter_none_molecules(
        idx_none: Iterable,
        *args: Union[pd.DataFrame, pd.Series, np.ndarray, torch.Tensor, list, tuple, Dict[Any, Iterable]],
    ) -> List[Union[pd.DataFrame, pd.Series, np.ndarray, torch.Tensor, list, tuple, Dict[Any, Iterable]]]:
        """
        Filter the molecules, labels, etc. for the molecules that failed featurization.

        Parameters:
            idx_none: A list of the indexes that failed featurization
            args: Any argument from which to filter the failed SMILES.
                Can be a `list`, `tuple`, `Tensor`, `np.array`, `Dict`, `pd.DataFrame`, `pd.Series`.
                Otherwise, it is not filtered.
                WARNING: If a `pd.DataFrame` or `pd.Series` is passed, it filters by the row indexes,
                NOT by the `DataFrame.index` or `Series.index`! Be careful!

        Returns:
            out: All the `args` with the indexes from `idx_none` removed.
        """
        if len(idx_none) == 0:
            return args
        idx_none = np.asarray(idx_none)

        out = []
        for arg in args:
            if isinstance(arg, pd.DataFrame):
                new = arg.drop(arg.index[idx_none], axis=0)
            elif isinstance(arg, pd.Series):
                new = arg.drop(arg.index[idx_none], axis=0)
            elif isinstance(arg, np.ndarray):
                new = np.delete(arg, idx_none, axis=0)
            elif isinstance(arg, torch.Tensor):
                not_none = torch.ones(arg.shape[0], dtype=bool)
                not_none[idx_none] = False
                new = arg[not_none]
            elif isinstance(arg, (list, tuple)):
                arg = list(arg)
                new = [elem for ii, elem in enumerate(arg) if ii not in idx_none]
            elif isinstance(arg, dict):
                new = {}
                for key, val in arg.items():
                    new[key] = MultitaskFromSmilesDataModule._filter_none_molecules(idx_none, val)  # Careful
            else:
                new = arg
            out.append(new)

        out = tuple(out) if len(out) > 1 else out[0]

        return out

    def _parse_label_cols(
        self,
        df: pd.DataFrame,
        df_path: Optional[Union[str, os.PathLike]],
        label_cols: Union[Type[None], str, List[str]],
        smiles_col: str,
    ) -> List[str]:
        r"""
        Parse the choice of label columns depending on the type of input.
        The input parameters `label_cols` and `smiles_col` are described in
        the `__init__` method.
        Parameters:
            df: The dataframe containing the labels.
            df_path: The path to the dataframe containing the labels.
            label_cols: The columns to use as labels.
            smiles_col: The column to use as SMILES
        Returns:
            the parsed label columns
        """
        if df is None:
            files = self._glob(df_path)
            if len(files) == 0:
                raise FileNotFoundError(f"No such file or directory `{df_path}`")

            cols = BaseDataModule._get_table_columns(files[0])
            for file in files[1:]:
                _cols = BaseDataModule._get_table_columns(file)
                if set(cols) != set(_cols):
                    raise RuntimeError(
                        f"Multiple data files have different columns. \nColumn set 1: {cols}\nColumn set 2: {_cols}"
                    )
        else:
            cols = list(df.columns)

        # A star `*` at the beginning or end of the string specifies to look for all
        # columns that starts/end with a specific string
        if isinstance(label_cols, str):
            if label_cols[0] == "*":
                label_cols = [col for col in cols if str(col).endswith(label_cols[1:])]
            elif label_cols[-1] == "*":
                label_cols = [col for col in cols if str(col).startswith(label_cols[:-1])]
            else:
                label_cols = [label_cols]

        elif label_cols is None:
            label_cols = [col for col in cols if col != smiles_col]

        return check_arg_iterator(label_cols, enforce_type=list)

    @property
    def is_prepared(self):
        if not hasattr(self, "dataset"):
            return False
        return getattr(self, "dataset") is not None

    @property
    def is_setup(self):
        if not (hasattr(self, "train_ds") or hasattr(self, "test_ds")):
            return False
        return (getattr(self, "train_ds") is not None) or (getattr(self, "test_ds") is not None)

    @property
    def num_node_feats(self):
        """Return the number of node features in the first graph"""
        graph = self.get_fake_graph()
        num_feats = graph.feat.shape[1]
        return num_feats

    @property
    def in_dims(self):
        """
        Return all input dimensions for the set of graphs.
        Including node/edge features, and
        raw positional encoding dimensions such eigval, eigvec, rwse and more
        """

        graph = self.get_fake_graph()
        if isinstance(graph, (GraphDict)):
            graph = graph.data

        # get list of all keys corresponding to positional encoding
        pe_dim_dict = {}
        g_keys = graph.keys

        # ignore the normal keys for node feat and edge feat etc.
        for key in g_keys:
            prop = graph.get(key, None)
            if hasattr(prop, "shape"):
                pe_dim_dict[key] = prop.shape[-1]
        return pe_dim_dict

    @property
    def num_edge_feats(self):
        """Return the number of edge features in the first graph"""

        graph = self.get_fake_graph()
        empty = torch.Tensor([])
        num_feats = graph.get("edge_feat", empty).shape[-1]

        return num_feats

    def get_fake_graph(self):
        """
        Low memory footprint method to get the featurization of a fake graph
        without reading the dataset. Useful for getting the number of node/edge features.

        Returns:
            graph: A fake graph with the right featurization
        """

        smiles = "C1=CC=CC=C1"
        trans = deepcopy(self.smiles_transformer)
        trans.keywords.setdefault("on_error", "raise")
        trans.keywords.setdefault("mask_nan", 0.0)
        graph = trans(smiles)
        return graph

    ########################## Private methods ######################################
    def _save_to_cache(self):
        raise NotImplementedError()

    def _load_from_cache(self):
        raise NotImplementedError()

    def _extract_smiles_labels(
        self,
        df: pd.DataFrame,
        smiles_col: str = None,
        label_cols: List[str] = [],
        idx_col: str = None,
        weights_col: str = None,
        weights_type: str = None,
    ) -> Tuple[
        np.ndarray, np.ndarray, Union[Type[None], np.ndarray], Dict[str, Union[Type[None], np.ndarray]]
    ]:
        """
        For a given dataframe extract the SMILES and labels columns. Smiles is returned as a list
        of string while labels are returned as a 2D numpy array.

        Parameters:
            df: Pandas dataframe
            smiles_col: Name of the column containing the SMILES
            label_cols: List of column names containing the labels
            idx_col: Name of the column containing the index
            weights_col: Name of the column containing the weights
            weights_type: Type of weights to use.
        Returns:
            smiles, labels, sample_idx, extras
        """

        if smiles_col is None:  # Should we specify which dataset has caused the potential issue?
            smiles_col_all = [col for col in df.columns if "smile" in str(col).lower()]
            if len(smiles_col_all) == 0:
                raise ValueError(f"No SMILES column found in dataframe. Columns are {df.columns}")
            elif len(smiles_col_all) > 1:
                raise ValueError(
                    f"Multiple SMILES column found in dataframe. SMILES Columns are {smiles_col_all}"
                )

            smiles_col = smiles_col_all[0]

        if label_cols is None:
            label_cols = df.columns.drop(smiles_col)

        label_cols = check_arg_iterator(label_cols, enforce_type=list)
        smiles = df[smiles_col].values
        if len(label_cols) > 0:
            labels = [pd.to_numeric(df[col], errors="coerce") for col in label_cols]
            labels = np.stack(labels, axis=1)
        else:
            labels = np.zeros([len(smiles), 0])

        indices = None  # What are indices for?
        if idx_col is not None:
            indices = df[idx_col].values

        sample_idx = df.index.values

        # Extract the weights
        weights = None
        if weights_col is not None:
            weights = df[weights_col].values
        elif weights_type is not None:
            if not np.all((labels == 0) | (labels == 1)):
                raise ValueError("Labels must be binary for `weights_type`")

            if weights_type == "sample_label_balanced":
                ratio_pos_neg = np.sum(labels, axis=0, keepdims=1) / labels.shape[0]
                weights = np.zeros(labels.shape)
                weights[labels == 0] = ratio_pos_neg
                weights[labels == 1] = ratio_pos_neg**-1

            elif weights_type == "sample_balanced":
                ratio_pos_neg = np.sum(labels, axis=0, keepdims=1) / labels.shape[0]
                weights = np.zeros(labels.shape)
                weights[labels == 0] = ratio_pos_neg
                weights[labels == 1] = ratio_pos_neg**-1
                weights = np.prod(weights, axis=1)

            else:
                raise ValueError(f"Undefined `weights_type` {weights_type}")

            weights /= np.max(weights)  # Put the max weight to 1

        extras = {"indices": indices, "weights": weights}
        return smiles, labels, sample_idx, extras

    def _get_split_indices(
        self,
        dataset_size: int,
        split_val: float,
        split_test: float,
        sample_idx: Optional[Iterable[int]] = None,
        split_seed: int = None,
        splits_path: Union[str, os.PathLike] = None,
        split_names: Optional[List[str]] = ["train", "val", "test"],
    ):
        r"""
        Compute indices of random splits.
        Parameters:
            dataset_size: Size of the dataset
            split_val: Fraction of the dataset to use for validation
            split_test: Fraction of the dataset to use for testing
            sample_idx: Indices of the samples to use for splitting
            split_seed: Seed for the random splitting
            splits_path: Path to a file containing the splits
        Returns:
            train_indices, val_indices, test_indices
        """

        if sample_idx is None:
            sample_idx = np.arange(dataset_size)

        if splits_path is None:
            # Random splitting
            if split_test + split_val > 0:
                train_indices, val_test_indices = train_test_split(
                    sample_idx,
                    test_size=split_val + split_test,
                    random_state=split_seed,
                )
                sub_split_test = split_test / (split_test + split_val)
            else:
                train_indices = sample_idx
                val_test_indices = np.array([])
                sub_split_test = 0

            if split_test > 0:
                val_indices, test_indices = train_test_split(
                    val_test_indices,
                    test_size=sub_split_test,
                    random_state=split_seed,
                )
            else:
                val_indices = val_test_indices
                test_indices = np.array([])

        else:
            # Split from an indices file
            file_type = self._get_data_file_type(splits_path)

            if file_type == "pt":
                splits = torch.load(splits_path)
            elif file_type in ["csv", "tsv"]:
                with fsspec.open(str(splits_path)) as f:
                    splits = self._read_csv(splits_path)
                splits = splits.dropna()
            else:
                raise ValueError(
                    f"file type `{file_type}` for `{splits_path}` not recognised, please use .pt, .csv or .tsv"
                )
            train, val, test = split_names
            train_indices = splits[train].astype("int").tolist()
            val_indices = splits[val].astype("int").tolist()
            test_indices = splits[test].astype("int").tolist()

        # Filter train, val and test indices
        _, train_idx, _ = np.intersect1d(sample_idx, train_indices, return_indices=True)
        train_indices = train_idx.tolist()
        _, valid_idx, _ = np.intersect1d(sample_idx, val_indices, return_indices=True)
        val_indices = valid_idx.tolist()
        _, test_idx, _ = np.intersect1d(sample_idx, test_indices, return_indices=True)
        test_indices = test_idx.tolist()

        return train_indices, val_indices, test_indices

    def _sub_sample_df(
        self, df: pd.DataFrame, sample_size: Union[int, float, None], seed: Optional[int] = None
    ) -> pd.DataFrame:
        r"""
        subsample from a pandas dataframe
        Parameters:
            df: pandas dataframe to subsample
            sample_size: number of samples to subsample
        Returns:
            subsampled pandas dataframe
        """
        # Sub-sample the dataframe
        if isinstance(sample_size, int):
            n = min(sample_size, df.shape[0])
            df = df.sample(n=n, random_state=seed)
        elif isinstance(sample_size, float):
            df = df.sample(f=sample_size, random_state=seed)
        elif sample_size is None:
            pass
        else:
            raise ValueError(
                f"Wrong value for `sample_size`: {sample_size}"
            )  # Maybe specify which task it was for?

        return df

    def get_data_hash(self):
        """
        Get a hash specific to a dataset and smiles_transformer.
        Useful to cache the pre-processed data.
        """
        hash_dict = {
            "smiles_transformer": self.smiles_transformer,
            "task_specific_args": self.task_specific_args,
        }
        data_hash = get_md5_hash(hash_dict)
        return data_hash

    def get_data_cache_fullname(self, compress: bool = False) -> str:
        """
        Create a hash for the dataset, and use it to generate a file name

        Parameters:
            compress: Whether to compress the data
        Returns:
            full path to the data cache file
        """
        if self.cache_data_path is None:
            return
        ext = ".datacache"
        if compress:
            ext += ".gz"
        data_cache_fullname = fs.join(self.cache_data_path, self.data_hash + ext)
        return data_cache_fullname

    def save_data_to_cache(self, verbose: bool = True, compress: bool = False) -> None:
        """
        Save the datasets from cache. First create a hash for the dataset, use it to
        generate a file name. Then save to the path given by `self.cache_data_path`.

        Parameters:
            verbose: Whether to print the progress
            compress: Whether to compress the data

        """
        full_cache_data_path = self.get_data_cache_fullname(compress=compress)
        if full_cache_data_path is None:
            logger.info("No cache data path specified. Skipping saving the data to cache.")
            return

        save_params = {
            "single_task_datasets": self.single_task_datasets,
            "task_train_indices": self.task_train_indices,
            "task_val_indices": self.task_val_indices,
            "task_test_indices": self.task_test_indices,
        }

        fs.mkdir(self.cache_data_path)
        with fsspec.open(full_cache_data_path, mode="wb", compression="infer") as file:
            if verbose:
                logger.info(f"Saving the data to cache at path:\n`{full_cache_data_path}`")
            now = time.time()
            torch.save(save_params, file)
            elapsed = round(time.time() - now)
            if verbose:
                logger.info(
                    f"Successfully saved the data to cache in {elapsed}s at path: `{full_cache_data_path}`"
                )

    def load_data_from_cache(self, verbose: bool = True, compress: bool = False) -> bool:
        """
        Load the datasets from cache. First create a hash for the dataset, and verify if that
        hash is available at the path given by `self.cache_data_path`.

        Parameters:
            verbose: Whether to print the progress
            compress: Whether to compress the data

        Returns:
            cache_data_exists: Whether the cache exists (if the hash matches) and the loading succeeded
        """
        full_cache_data_path = self.get_data_cache_fullname(compress=compress)

        if full_cache_data_path is None:
            logger.info("No cache data path specified. Skipping loading the data from cache.")
            return False

        cache_data_exists = fs.exists(full_cache_data_path)

        if cache_data_exists:
            try:
                logger.info(f"Loading the data from cache at path `{full_cache_data_path}`")
                now = time.time()
                with fsspec.open(full_cache_data_path, mode="rb", compression="infer") as file:
                    load_params = torch.load(file)
                    self.__dict__.update(load_params)
                    (
                        self.train_singletask_datasets,
                        self.val_singletask_datasets,
                        self.test_singletask_datasets,
                    ) = self.get_subsets_of_datasets(
                        self.single_task_datasets,
                        self.task_train_indices,
                        self.task_val_indices,
                        self.task_test_indices,
                    )
                elapsed = round(time.time() - now)
                logger.info(
                    f"Successfully loaded the data from cache in {elapsed}s at path: `{full_cache_data_path}`"
                )
                return True
            except Exception as e:
                if verbose:
                    logger.warning(
                        f"Data cache failed to load path: `{full_cache_data_path}`.\nThe data will be prepared and cache will be created for future runs."
                    )
                    logger.warning(e.__str__())
                return False
        else:
            if verbose:
                logger.info(
                    f"Data cache not found at path: `{full_cache_data_path}`.\nThe data will be prepared and cache will be created for future runs."
                )
            return False

    def get_subsets_of_datasets(
        self,
        single_task_datasets: Dict[str, Datasets.SingleTaskDataset],
        task_train_indices: Dict[str, Iterable],
        task_val_indices: Dict[str, Iterable],
        task_test_indices: Dict[str, Iterable],
    ) -> Tuple[Subset, Subset, Subset]:
        """
        From a dictionary of datasets and their associated indices, subset the train/val/test sets

        Parameters:
            single_task_datasets: Dictionary of datasets
            task_train_indices: Dictionary of train indices
            task_val_indices: Dictionary of val indices
            task_test_indices: Dictionary of test indices
        Returns:
            train_singletask_datasets: Dictionary of train subsets
            val_singletask_datasets: Dictionary of val subsets
            test_singletask_datasets: Dictionary of test subsets
        """
        train_singletask_datasets = {}
        val_singletask_datasets = {}
        test_singletask_datasets = {}
        for task in task_train_indices.keys():
            train_singletask_datasets[task] = Subset(single_task_datasets[task], task_train_indices[task])
            val_singletask_datasets[task] = Subset(single_task_datasets[task], task_val_indices[task])
            test_singletask_datasets[task] = Subset(single_task_datasets[task], task_test_indices[task])
        return train_singletask_datasets, val_singletask_datasets, test_singletask_datasets

    def __len__(self) -> int:
        r"""
        Returns the number of elements of the current DataModule, which is the combined size of all single-task datasets given.
        Returns:
            num_elements: Number of elements in the current DataModule
        """
        num_elements = 0
        for task, args in self.task_dataset_processing_params.items():
            if args.df is None:
                df = self._read_csv(args.df_path, usecols=[args.smiles_col])
                num_elements += len(df)
            else:
                num_elements += len(args.df)
        return num_elements

    def to_dict(self) -> Dict[str, Any]:
        """
        Returns a dictionary representation of the current DataModule
        Returns:
            obj_repr: Dictionary representation of the current DataModule
        """
        # TODO: Change to make more multi-task friendly
        obj_repr = {}
        obj_repr["name"] = self.__class__.__name__
        obj_repr["len"] = len(self)
        obj_repr["train_size"] = len(self.train_indices) if self.train_indices is not None else None
        obj_repr["val_size"] = len(self.val_indices) if self.val_indices is not None else None
        obj_repr["test_size"] = len(self.test_indices) if self.test_indices is not None else None
        obj_repr["batch_size_training"] = self.batch_size_training
        obj_repr["batch_size_inference"] = self.batch_size_inference
        obj_repr["batch_size_per_pack"] = self.batch_size_per_pack
        obj_repr["num_node_feats"] = self.num_node_feats
        obj_repr["num_node_feats_with_positional_encoding"] = self.num_node_feats_with_positional_encoding
        obj_repr["num_edge_feats"] = self.num_edge_feats
        obj_repr["num_tasks"] = len(self.task_dataset_processing_params)
        obj_repr["num_labels"] = len(self.label_cols)
        obj_repr["collate_fn"] = self.collate_fn.__name__
        obj_repr["featurization"] = self.featurization
        return obj_repr

    def __repr__(self) -> str:
        r"""
        Controls how the class is printed

        Returns:

        """
        return omegaconf.OmegaConf.to_yaml(self.to_dict())


class GraphOGBDataModule(MultitaskFromSmilesDataModule):
    def __init__(
        self,
        task_specific_args: Dict[str, Dict[str, Any]],  # TODO: Replace this with DatasetParams
        cache_data_path: Optional[Union[str, os.PathLike]] = None,
        featurization: Optional[Union[Dict[str, Any], omegaconf.DictConfig]] = None,
        batch_size_training: int = 16,
        batch_size_inference: int = 16,
        batch_size_per_pack: Optional[int] = None,
        num_workers: int = 0,
        pin_memory: bool = True,
        persistent_workers: bool = False,
        featurization_n_jobs: int = -1,
        featurization_progress: bool = False,
        featurization_backend: str = "loky",
        collate_fn: Optional[Callable] = None,
        prepare_dict_or_graph: str = "pyg:graph",
        **kwargs,
    ):
        r"""
        Load an OGB (Open-graph-benchmark) GraphProp dataset.

        Parameters:
            task_specific_args: Arguments related to each task, with the task-name being the key,
              and the specific arguments being the values. The arguments must be a
              Dict containing the following keys:

              - "dataset_name": Name of the OGB dataset to load. Examples of possible datasets are
                "ogbg-molhiv", "ogbg-molpcba", "ogbg-moltox21", "ogbg-molfreesolv".
              - "sample_size": The number of molecules to sample from the dataset. Default=None,
                meaning that all molecules will be considered.
            cache_data_path: path where to save or reload the cached data. The path can be
                remote (S3, GS, etc).
            featurization: args to apply to the SMILES to Graph featurizer.
            batch_size_training: batch size for training and val dataset.
            batch_size_inference: batch size for test dataset.
            num_workers: Number of workers for the dataloader. Use -1 to use all available
                cores.
            pin_memory: Whether to pin on paginated CPU memory for the dataloader.
            featurization_n_jobs: Number of cores to use for the featurization.
            featurization_progress: whether to show a progress bar during featurization.
            featurization_backend: The backend to use for the molecular featurization.

                - "multiprocessing": Found to cause less memory issues.
                - "loky": joblib's Default. Found to cause memory leaks.
                - "threading": Found to be slow.

            collate_fn: A custom torch collate function. Default is to `goli.data.goli_collate_fn`
            sample_size:

                - `int`: The maximum number of elements to take from the dataset.
                - `float`: Value between 0 and 1 representing the fraction of the dataset to consider
                - `None`: all elements are considered.
        """

        new_task_specific_args = {}
        self.metadata = {}
        for task_name, task_args in task_specific_args.items():
            # Get OGB metadata
            this_metadata = self._get_dataset_metadata(task_args["dataset_name"])
            # Get dataset
            df, idx_col, smiles_col, label_cols, splits_path = self._load_dataset(
                this_metadata, sample_size=task_args.get("sample_size", None)
            )
            new_task_specific_args[task_name] = {
                "df": df,
                "idx_col": idx_col,
                "smiles_col": smiles_col,
                "label_cols": label_cols,
                "splits_path": splits_path,
            }
            self.metadata[task_name] = this_metadata

        # Config for datamodule
        dm_args = {}
        dm_args["task_specific_args"] = new_task_specific_args
        dm_args["cache_data_path"] = cache_data_path
        dm_args["featurization"] = featurization
        dm_args["batch_size_training"] = batch_size_training
        dm_args["batch_size_inference"] = batch_size_inference
        dm_args["batch_size_per_pack"] = batch_size_per_pack
        dm_args["num_workers"] = num_workers
        dm_args["pin_memory"] = pin_memory
        dm_args["featurization_n_jobs"] = featurization_n_jobs
        dm_args["featurization_progress"] = featurization_progress
        dm_args["featurization_backend"] = featurization_backend
        dm_args["persistent_workers"] = persistent_workers
        dm_args["collate_fn"] = collate_fn
        dm_args["prepare_dict_or_graph"] = prepare_dict_or_graph

        super().__init__(**dm_args, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        r"""
        geenrate a dictionary representation of the class
        Returns:
            dict: dictionary representation of the class
        """
        # TODO: Change to make more multi-task friendly
        obj_repr = {}
        obj_repr["dataset_name"] = self.dataset_name
        obj_repr.update(super().to_dict())
        return obj_repr

    # Private methods

    def _load_dataset(
        self,
        metadata: dict,
        sample_size: Optional[int] = None,
    ) -> Tuple[pd.DataFrame, str, str, List[str], str]:
        """
        Download, extract and load an OGB dataset.
        Parameters:
            metadata: Metadata for the dataset to load.
            sample_size: The number of molecules to sample from the dataset. Default=None,
                meaning that all molecules will be considered.
        Returns:
            df: Pandas dataframe containing the dataset.
            idx_col: Name of the column containing the molecule index.
            smiles_col: Name of the column containing the SMILES.
            label_cols: List of column names containing the labels.
            splits_path: Path to the file containing the train/val/test splits.
        """

        base_dir = fs.get_cache_dir("ogb")
        dataset_dir = base_dir / metadata["download_name"]

        if not dataset_dir.exists():
            # Create cache filepath for zip file and associated folder
            dataset_path = base_dir / f"{metadata['download_name']}.zip"

            # Download it
            if not dataset_path.exists():
                logger.info(f"Downloading {metadata['url']} to {dataset_path}")
                fs.copy(metadata["url"], dataset_path, progress=True)

            # Extract
            zf = zipfile.ZipFile(dataset_path)
            zf.extractall(base_dir)

        # Load CSV file
        if metadata["download_name"].startswith("pcqm4m"):
            df_path = dataset_dir / "raw" / "data.csv.gz"
        else:
            df_path = dataset_dir / "mapping" / "mol.csv.gz"
        logger.info(f"Loading {df_path} in memory.")
        df = pd.read_csv(df_path)

        # Subsample the dataset
        df = self._sub_sample_df(df, sample_size)

        # Load split from the OGB dataset and save them in a single CSV file
        if metadata["download_name"].startswith("pcqm4m"):
            split_name = metadata["split"]
            split_dict = torch.load(dataset_dir / "split_dict.pt")
            train_split = pd.DataFrame(split_dict["train"])
            val_split = pd.DataFrame(split_dict["valid"])
            if "test" in split_dict.keys():
                test_split = pd.DataFrame(split_dict["test"])
            else:
                test_split = pd.DataFrame(split_dict["test-dev"])

            splits = pd.concat([train_split, val_split, test_split], axis=1)  # type: ignore
            splits.columns = ["train", "val", "test"]

            splits_path = dataset_dir / "split"
            if not splits_path.exists():
                os.makedirs(splits_path)
                splits_path = dataset_dir / f"{split_name}.csv.gz"
            else:
                splits_path = splits_path / f"{split_name}.csv.gz"
        else:
            split_name = metadata["split"]
            train_split = pd.read_csv(dataset_dir / "split" / split_name / "train.csv.gz", header=None)  # type: ignore
            val_split = pd.read_csv(dataset_dir / "split" / split_name / "valid.csv.gz", header=None)  # type: ignore
            test_split = pd.read_csv(dataset_dir / "split" / split_name / "test.csv.gz", header=None)  # type: ignore

            splits = pd.concat([train_split, val_split, test_split], axis=1)  # type: ignore
            splits.columns = ["train", "val", "test"]

            splits_path = dataset_dir / "split" / f"{split_name}.csv.gz"

        logger.info(f"Saving splits to {splits_path}")
        splits.to_csv(splits_path, index=None)

        # Get column names: OGB columns are predictable
        if metadata["download_name"].startswith("pcqm4m"):
            idx_col = df.columns[0]
            smiles_col = df.columns[-2]
            label_cols = df.columns[-1:].to_list()
        else:
            idx_col = df.columns[-1]
            smiles_col = df.columns[-2]
            label_cols = df.columns[:-2].to_list()

        return df, idx_col, smiles_col, label_cols, splits_path

    def _get_dataset_metadata(self, dataset_name: str) -> Dict[str, Any]:
        ogb_metadata = self._get_ogb_metadata()
        if dataset_name not in ogb_metadata.index:
            raise ValueError(f"'{dataset_name}' is not a valid dataset name.")

        return ogb_metadata.loc[dataset_name].to_dict()

    def _get_ogb_metadata(self):
        """
        Get the metadata of OGB GraphProp datasets.
        """

        with importlib.resources.open_text("ogb.graphproppred", "master.csv") as f:
            ogb_metadata = pd.read_csv(f)
        ogb_metadata = ogb_metadata.set_index(ogb_metadata.columns[0])
        ogb_metadata = ogb_metadata.T

        # Add metadata related to PCQM4M
        ogb_metadata = pd.concat([ogb_metadata, pd.DataFrame(PCQM4M_meta, index=["ogbg-lsc-pcqm4m"])])
        ogb_metadata = pd.concat([ogb_metadata, pd.DataFrame(PCQM4Mv2_meta, index=["ogbg-lsc-pcqm4mv2"])])

        # Only keep datasets of type 'mol'
        ogb_metadata = ogb_metadata[ogb_metadata["data type"] == "mol"]

        return ogb_metadata


class FakeDataModule(MultitaskFromSmilesDataModule):
    """
    A fake datamodule that generates artificial data by mimicking the true data coming
    from the provided dataset.
    It is useful to test the speed and performance of the model on a dataset without
    having to featurize it and wait for the workers to load it.
    """

    def __init__(
        self,
        task_specific_args: Dict[str, Dict[str, Any]],  # TODO: Replace this with DatasetParams
        cache_data_path: Optional[Union[str, os.PathLike]] = None,
        featurization: Optional[Union[Dict[str, Any], omegaconf.DictConfig]] = None,
        batch_size_training: int = 16,
        batch_size_inference: int = 16,
        num_workers: int = 0,
        pin_memory: bool = True,
        persistent_workers: bool = False,
        collate_fn: Optional[Callable] = None,
        prepare_dict_or_graph: str = "pyg:graph",
        num_mols_to_generate: int = 1000000,
        indexing_single_elem: bool = True,
        **kwargs,
    ):
        super().__init__(
            task_specific_args=task_specific_args,
            cache_data_path=cache_data_path,
            featurization=featurization,
            batch_size_training=batch_size_training,
            batch_size_inference=batch_size_inference,
            num_workers=num_workers,
            pin_memory=pin_memory,
            persistent_workers=persistent_workers,
            collate_fn=collate_fn,
            prepare_dict_or_graph=prepare_dict_or_graph,
            **kwargs,
        )
        self.num_mols_to_generate = num_mols_to_generate
        self.indexing_single_elem = indexing_single_elem

    def generate_data(self, label_cols: List[str], smiles_col: str):
        """
        Parameters:
            labels_cols
            smiles_col
        Returns:
            pd.DataFrame
        """
        num_generated_mols = int(1)
        # Create a dummy generated dataset - singel smiles string, duplicated N times
        example_molecules = dict(
            smiles="C1N2C3C4C5OC13C2C45",
            cxsmiles="[H]C1C2=C(NC(=O)[C@@]1([H])C1=C([H])C([H])=C(C([H])([H])[H])C([H])=C1[H])C([H])=C([H])N=C2[H] |(6.4528,-1.5789,-1.2859;5.789,-0.835,-0.8455;4.8499,-0.2104,-1.5946;3.9134,0.7241,-0.934;3.9796,1.1019,0.3172;5.0405,0.6404,1.1008;5.2985,1.1457,2.1772;5.9121,-0.5519,0.613;6.9467,-0.2303,0.8014;5.677,-1.7955,1.4745;4.7751,-2.7953,1.0929;4.2336,-2.7113,0.154;4.5521,-3.9001,1.914;3.8445,-4.6636,1.5979;5.215,-4.0391,3.1392;4.9919,-5.2514,4.0126;5.1819,-5.0262,5.0671;5.6619,-6.0746,3.7296;3.966,-5.6247,3.925;6.1051,-3.0257,3.52;6.6247,-3.101,4.4725;6.3372,-1.9217,2.7029;7.0168,-1.1395,3.0281;2.8586,1.2252,-1.7853;2.1303,1.9004,-1.3493;2.8118,0.8707,-3.0956;2.0282,1.2549,-3.7434;3.716,0.0207,-3.7371;4.6658,-0.476,-3.0127;5.3755,-1.1468,-3.5021)|",
        )
        example_df_entry = {smiles_col: example_molecules[smiles_col]}
        for label in label_cols:
            example_df_entry[label] = np.random.random()
        df = pd.DataFrame([example_df_entry])
        logger.info(f"Generating fake dataset on host... \n Generating {num_generated_mols} rows in the df.")
        df = pd.concat([df] * num_generated_mols, ignore_index=True)
        return df

    def prepare_data(self):
        """Called only from a single process in distributed settings. Steps:

        - If each cache is set and exists, reload from cache and return. Otherwise,
        - For each single-task dataset:
            - Load its dataframe from a path (if provided)
            - Subsample the dataframe
            - Extract the smiles, labels from the dataframe
        - In the previous step, we were also able to get the unique smiles, which we use to compute the features
        - For each single-task dataframe and associated data (smiles, labels, etc.):
            - Filter out the data corresponding to molecules which failed featurization.
            - Create a corresponding SingletaskDataset
            - Split the SingletaskDataset according to the task-specific splits for train, val and test
        """

        """Load all single-task dataframes."""
        if self.num_mols_to_generate is None:
            num_mols = 0

        task_df = {}
        for task, args in self.task_dataset_processing_params.items():
            logger.info(f"Reading data for task '{task}'")
            if args.df is None:
                # Only load the useful columns, as some datasets can be very large when loading all columns.
                label_cols = self._parse_label_cols(
                    df=None, df_path=args.df_path, label_cols=args.label_cols, smiles_col=args.smiles_col
                )
                task_df[task] = self.generate_data(label_cols=args.label_cols, smiles_col=args.smiles_col)
                if self.num_mols_to_generate is None:
                    num_mols = max(num_mols, len(task_df[task]))
            task_df[task] = task_df[task].iloc[0:1]

            args.label_cols = label_cols
        if self.num_mols_to_generate is None:
            self.num_mols_to_generate = num_mols
        logger.info("Done reading datasets")

        """Subsample the data frames and extract the necessary data to create SingleTaskDatasets for each task (smiles, labels, extras)."""
        task_dataset_args = {}
        for task in task_df.keys():
            task_dataset_args[task] = {}

        for task, df in task_df.items():
            logger.info(f"Prepare single-task dataset for task '{task}' with {len(df)} data points.")
            # Extract smiles, labels, extras
            args = self.task_dataset_processing_params[task]
            smiles, labels, sample_idx, extras = self._extract_smiles_labels(
                df,
                smiles_col=args.smiles_col,
                label_cols=args.label_cols,
                idx_col=args.idx_col,
                weights_col=args.weights_col,
                weights_type=args.weights_type,
            )

            # Store the relevant information for each task's dataset
            task_dataset_args[task]["smiles"] = smiles
            task_dataset_args[task]["labels"] = labels
            task_dataset_args[task]["sample_idx"] = sample_idx
            task_dataset_args[task]["extras"] = extras

        """Convert SMILES to features (graphs, fingerprints, etc.) for the unique molecules found."""
        all_smiles = []
        idx_per_task = {}
        total_len = 0
        for task, dataset_args in task_dataset_args.items():
            all_smiles.extend(dataset_args["smiles"])
            num_smiles = len(dataset_args["smiles"])
            idx_per_task[task] = (total_len, total_len + num_smiles)
            total_len += num_smiles
        # Get all unique mol ids
        all_mol_ids = smiles_to_unique_mol_ids(
            all_smiles,
            n_jobs=self.featurization_n_jobs,
            featurization_batch_size=self.featurization_batch_size,
            backend=self.featurization_backend,
        )
        # Convert SMILES to features
        features, _ = self._featurize_molecules(all_smiles)
        task_dataset_args[task]["features"] = features
        """Filter data based on molecules which failed featurization. Create single task datasets as well."""
        self.single_task_datasets = {}
        for task, args in task_dataset_args.items():
            self.single_task_datasets[task] = Datasets.SingleTaskDataset(
                features=task_dataset_args[task]["features"],
                labels=task_dataset_args[task]["labels"],
                smiles=task_dataset_args[task]["smiles"],
                unique_ids=all_mol_ids[idx_per_task[task][0] : idx_per_task[task][1]],
                **task_dataset_args[task]["extras"],
            )

        """We split the data up to create train, val and test datasets"""
        self.train_singletask_datasets = {}
        self.val_singletask_datasets = {}
        self.test_singletask_datasets = {}
        for task, df in task_df.items():
            self.train_singletask_datasets[task] = Subset(self.single_task_datasets[task], [0])
            self.val_singletask_datasets[task] = Subset(self.single_task_datasets[task], [0])
            self.test_singletask_datasets[task] = Subset(self.single_task_datasets[task], [0])

    def setup(self, stage=None):
        # TODO
        """
        Prepare the torch dataset. Called on every GPUs. Setting state here is ok.
        Parameters:
            stage (str): Either 'fit', 'test', or None.
        """
        labels_size = {}

        if stage == "fit" or stage is None:
            self.train_ds = Datasets.FakeDataset(self.train_singletask_datasets, num_mols=self.num_mols_to_generate, indexing_same_elem=self.indexing_single_elem)  # type: ignore
            self.val_ds = Datasets.FakeDataset(self.val_singletask_datasets, num_mols=self.num_mols_to_generate, indexing_same_elem=self.indexing_single_elem)  # type: ignore
            print(self.train_ds)
            print(self.val_ds)

            labels_size.update(
                self.train_ds.labels_size
            )  # Make sure that all task label sizes are contained in here. Maybe do the update outside these if statements.
            labels_size.update(self.val_ds.labels_size)

        if stage == "test" or stage is None:
            self.test_ds = Datasets.FakeDataset(self.test_singletask_datasets, num_mols=self.num_mols_to_generate, indexing_same_elem=self.indexing_single_elem)  # type: ignore
            print(self.test_ds)
            labels_size.update(self.test_ds.labels_size)

        default_labels_size_dict = self.collate_fn.keywords.get("labels_size_dict", None)

        if default_labels_size_dict is None:
            self.collate_fn.keywords["labels_size_dict"] = labels_size

    def get_fake_graph(self):
        """
        Low memory footprint method to get the first datapoint DGL graph.
        The first 10 rows of the data are read in case the first one has a featurization
        error. If all 20 first element, then `None` is returned, otherwise the first
        graph to not fail is returned.
        """
        keys = list(self.task_dataset_processing_params.keys())
        task = keys[0]
        args = self.task_dataset_processing_params[task]
        if args.df is None:
            df = self._read_csv(args.df_path, nrows=20)
        else:
            df = args.df.iloc[0:20, :]

        df = df.iloc[0:20, :]
        label_cols = self._parse_label_cols(
            df, df_path=None, label_cols=args.label_cols, smiles_col=args.smiles_col
        )

        smiles, labels, sample_idx, extras = self._extract_smiles_labels(
            df,
            smiles_col=args.smiles_col,
            label_cols=label_cols,
            idx_col=args.idx_col,
            weights_col=args.weights_col,
            weights_type=args.weights_type,
        )

        graph = None
        for s in smiles:
            graph = self.smiles_transformer(s, mask_nan=0.0)
            num_nodes = graph.num_nodes
            num_edges = graph.num_edges
            if (graph is not None) and (num_edges > 0) and (num_nodes > 0):
                break
        return graph

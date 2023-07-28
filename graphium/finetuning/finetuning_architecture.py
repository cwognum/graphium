from typing import Iterable, List, Dict, Tuple, Union, Callable, Any, Optional, Type

from copy import deepcopy

from loguru import logger

import torch
import torch.nn as nn

from torch import Tensor
from torch_geometric.data import Batch

from graphium.data.utils import get_keys
from graphium.nn.base_graph_layer import BaseGraphStructure
from graphium.nn.architectures.encoder_manager import EncoderManager
from graphium.nn.architectures import FeedForwardNN, FeedForwardPyg, TaskHeads
from graphium.nn.architectures.global_architectures import FeedForwardGraph
from graphium.trainer.predictor_options import ModelOptions
from graphium.nn.utils import MupMixin

FINETUNING_HEAD_DICT = {
    "mlp": FeedForwardNN,
    "gnn": FeedForwardPyg,
    "task_head": TaskHeads
}


class FullGraphFinetuningNetwork(nn.Module, MupMixin):
    def __init__(
        self,
        gnn_kwargs: Dict[str, Any],
        pre_nn_kwargs: Optional[Dict[str, Any]] = None,
        pre_nn_edges_kwargs: Optional[Dict[str, Any]] = None,
        pe_encoders_kwargs: Optional[Dict[str, Any]] = None,
        task_heads_kwargs: Optional[Dict[str, Any]] = None,
        graph_output_nn_kwargs: Optional[Dict[str, Any]] = None,
        finetuning_head_kwargs: Optional[Dict[str, Any]] = None,
        # accelerator_kwargs: Optional[Dict[str, Any]] = None,
        num_inference_to_average: int = 1,
        last_layer_is_readout: bool = False,
        name: str = "FullFinetuningGNN",
    ):
        r"""
        Class that allows to implement a full graph neural network architecture,
        including the pre-processing MLP and the post processing MLP.

        Parameters:

            gnn_kwargs:
                key-word arguments to use for the initialization of the pre-processing
                GNN network using the class `FeedForwardGraph`.
                It must respect the following criteria:

                - gnn_kwargs["in_dim"] must be equal to pre_nn_kwargs["out_dim"]
                - gnn_kwargs["out_dim"] must be equal to graph_output_nn_kwargs["in_dim"]

            pe_encoders_kwargs:
                key-word arguments to use for the initialization of all positional encoding encoders.
                See the class `EncoderManager` for more details.

            pre_nn_kwargs:
                key-word arguments to use for the initialization of the pre-processing
                MLP network of the node features before the GNN, using the class `FeedForwardNN`.
                If `None`, there won't be a pre-processing MLP.

            pre_nn_edges_kwargs:
                key-word arguments to use for the initialization of the pre-processing
                MLP network of the edge features before the GNN, using the class `FeedForwardNN`.
                If `None`, there won't be a pre-processing MLP.

            task_heads_kwargs:
                This argument is a list of dictionaries containing the arguments for task heads. Each argument is used to
                initialize a task-specific MLP.

            graph_output_nn_kwargs:
                This argument is a list of dictionaries corresponding to the arguments for a FeedForwardNN.
                Each dict of arguments is used to initialize a shared MLP.

            finetuning_head_kwargs:
                key-word arguments to use for the finetuning head.
                It must respect the following criteria:

                - [last_used_module]_kwarg["out_level"] must be equal to finetuning_head_kwargs["in_level"]
                - [last_used_module]_kwarg["out_dim"] must be equal to finetuning_head_kwargs["in_dim"]

                Here, [last_used_module] represents the module that is finetuned from,
                e.g., gnn, graph_output or (one of the) task_heads

            accelerator_kwargs:
                key-word arguments specific to the accelerator being used,
                e.g. pipeline split points

            num_inference_to_average:
                Number of inferences to average at val/test time. This is used to avoid the noise introduced
                by positional encodings with sign-flips. In case no such encoding is given,
                this parameter is ignored.
                NOTE: The inference time will be slowed-down proportionaly to this parameter.

            last_layer_is_readout: Whether the last layer should be treated as a readout layer.
                Allows to use the `mup.MuReadout` from the muTransfer method https://github.com/microsoft/mup

            name:
                Name attributed to the current network, for display and printing
                purposes.
        """
        
        super().__init__()
        
        # super().__init__(
        #     gnn_kwargs,
        #     pre_nn_kwargs,
        #     pre_nn_edges_kwargs,
        #     pe_encoders_kwargs,
        #     task_heads_kwargs,
        #     graph_output_nn_kwargs,
        #     accelerator_kwargs,
        #     num_inference_to_average,
        #     last_layer_is_readout,
        #     name
        # )

        self.name = name
        self.num_inference_to_average = num_inference_to_average
        self.last_layer_is_readout = last_layer_is_readout
        self._concat_last_layers = None
        self.pre_nn, self.pre_nn_edges, self.task_heads, self.finetuning_head = None, None, None, None
        self.pe_encoders_kwargs = deepcopy(pe_encoders_kwargs)
        self.graph_output_nn_kwargs = graph_output_nn_kwargs
        self.finetuning_head_kwargs = finetuning_head_kwargs
        self.encoder_manager = EncoderManager(pe_encoders_kwargs)
        self.max_num_nodes_per_graph = None
        self.max_num_edges_per_graph = None

        # Initialize the pre-processing neural net for nodes (applied directly on node features)
        if pre_nn_kwargs is not None:
            name = pre_nn_kwargs.pop("name", "pre-NN")
            self.pre_nn = FeedForwardNN(**pre_nn_kwargs, name=name)
            next_in_dim = self.pre_nn.out_dim
            gnn_kwargs.setdefault("in_dim", next_in_dim)
            assert (
                next_in_dim == gnn_kwargs["in_dim"]
            ), f"Inconsistent dimensions between pre-NN output ({next_in_dim}) and GNN input ({gnn_kwargs['in_dim']})"

        # Initialize the pre-processing neural net for edges (applied directly on edge features)
        if pre_nn_edges_kwargs is not None:
            name = pre_nn_edges_kwargs.pop("name", "pre-NN-edges")
            self.pre_nn_edges = FeedForwardNN(**pre_nn_edges_kwargs, name=name)
            next_in_dim = self.pre_nn_edges.out_dim
            gnn_kwargs.setdefault("in_dim_edges", next_in_dim)
            assert (
                next_in_dim == gnn_kwargs["in_dim_edges"]
            ), f"Inconsistent dimensions between pre-NN-edges output ({next_in_dim}) and GNN input ({gnn_kwargs['in_dim_edges']})"

        # Initialize the graph neural net (applied after the pre_nn)
        name = gnn_kwargs.pop("name", "GNN")
        gnn_class = FeedForwardGraph
        gnn_kwargs.setdefault(
            "last_layer_is_readout", self.last_layer_is_readout and (task_heads_kwargs is None)
        )
        self.gnn = gnn_class(**gnn_kwargs, name=name)
        next_in_dim = self.gnn.out_dim

        if task_heads_kwargs is not None:
            self.task_heads = TaskHeads(
                in_dim=self.out_dim,
                in_dim_edges=self.out_dim_edges,
                task_heads_kwargs=task_heads_kwargs,
                graph_output_nn_kwargs=graph_output_nn_kwargs,
            )
            self._task_heads_kwargs = task_heads_kwargs

        if finetuning_head_kwargs is not None:
            self.finetuning_head = FinetuningHead(finetuning_head_kwargs)

    def forward(self, g: Batch) -> Tensor:
        r"""
        Apply the pre-processing neural network, the graph neural network,
        and the post-processing neural network on the graph features.

        Parameters:

            g:
                pyg Batch graph on which the convolution is done.
                Must contain the following elements:

                - Node key `"feat"`: `torch.Tensor[..., N, Din]`.
                  Input node feature tensor, before the network.
                  `N` is the number of nodes, `Din` is the input features dimension ``self.pre_nn.in_dim``

                - Edge key `"edge_feat"`: `torch.Tensor[..., N, Ein]` **Optional**.
                  The edge features to use. It will be ignored if the
                  model doesn't supporte edge features or if
                  `self.in_dim_edges==0`.

                - Other keys related to positional encodings `"pos_enc_feats_sign_flip"`,
                  `"pos_enc_feats_no_flip"`.

        Returns:

            `torch.Tensor[..., M, Dout]` or `torch.Tensor[..., N, Dout]`:
                Node or graph feature tensor, after the network.
                `N` is the number of nodes, `M` is the number of graphs,
                `Dout` is the output dimension ``self.graph_output_nn.out_dim``
                If the `self.gnn.pooling` is [`None`], then it returns node features and the output dimension is `N`,
                otherwise it returns graph features and the output dimension is `M`

        """

        # Apply the positional encoders
        g = self.encoder_manager(g)

        g["feat"] = g["feat"]
        e = None

        if "edge_feat" in get_keys(g):
            g["edge_feat"] = g["edge_feat"]

        # Run the pre-processing network on node features
        if self.pre_nn is not None:
            g["feat"] = self.pre_nn.forward(g["feat"])

        # Run the pre-processing network on edge features
        # If there are no edges, skip the forward and change the dimension of e
        if self.pre_nn_edges is not None:
            e = g["edge_feat"]
            if torch.prod(torch.as_tensor(e.shape[:-1])) == 0:
                e = torch.zeros(
                    list(e.shape[:-1]) + [self.pre_nn_edges.out_dim], device=e.device, dtype=e.dtype
                )
            else:
                e = self.pre_nn_edges.forward(e)
            g["edge_feat"] = e

        # Run the graph neural network
        g = self.gnn.forward(g)

        if self.task_heads is not None:
            g = self.task_heads.forward(g)

        if self.finetuning_head is not None:
            g = self.finetuning_head.forward(g)

        return g

    def make_mup_base_kwargs(self, divide_factor: float = 2.0) -> Dict[str, Any]:
        """
        Create a 'base' model to be used by the `mup` or `muTransfer` scaling of the model.
        The base model is usually identical to the regular model, but with the
        layers width divided by a given factor (2 by default)

        Parameter:
            divide_factor: Factor by which to divide the width.

        Returns:
            Dictionary with the kwargs to create the base model.
        """
        kwargs = dict(
            gnn_kwargs=None,
            pre_nn_kwargs=None,
            pre_nn_edges_kwargs=None,
            pe_encoders_kwargs=None,
            finetuning_head_kwargs=None,
            num_inference_to_average=self.num_inference_to_average,
            last_layer_is_readout=self.last_layer_is_readout,
            name=self.name,
        )

        # For the pre-nn network, get the smaller dimensions.
        # For the input dim, only divide the features coming from the pe-encoders
        if self.pre_nn is not None:
            kwargs["pre_nn_kwargs"] = self.pre_nn.make_mup_base_kwargs(
                divide_factor=divide_factor, factor_in_dim=False
            )
            pe_enc_outdim = 0 if self.encoder_manager is None else self.pe_encoders_kwargs.get("out_dim", 0)
            pre_nn_indim = kwargs["pre_nn_kwargs"]["in_dim"] - pe_enc_outdim
            kwargs["pre_nn_kwargs"]["in_dim"] = round(pre_nn_indim + (pe_enc_outdim / divide_factor))

        # For the pre-nn on the edges, factor all dimensions, except the in_dim
        if self.pre_nn_edges is not None:
            kwargs["pre_nn_edges_kwargs"] = self.pre_nn_edges.make_mup_base_kwargs(
                divide_factor=divide_factor, factor_in_dim=False
            )
            pe_enc_edge_outdim = (
                0 if self.encoder_manager is None else self.pe_encoders_kwargs.get("edge_out_dim", 0)
            )
            pre_nn_edge_indim = kwargs["pre_nn_edges_kwargs"]["in_dim"] - pe_enc_edge_outdim
            kwargs["pre_nn_edges_kwargs"]["in_dim"] = round(
                pre_nn_edge_indim + (pe_enc_edge_outdim / divide_factor)
            )

        # For the pe-encoders, don't factor the in_dim and in_dim_edges
        if self.encoder_manager is not None:
            kwargs["pe_encoders_kwargs"] = self.encoder_manager.make_mup_base_kwargs(
                divide_factor=divide_factor
            )

        if self.task_heads is not None:
            task_heads_kwargs = self.task_heads.make_mup_base_kwargs(
                divide_factor=divide_factor, factor_in_dim=True
            )
            kwargs["task_heads_kwargs"] = task_heads_kwargs["task_heads_kwargs"]
            kwargs["graph_output_nn_kwargs"] = task_heads_kwargs["graph_output_nn_kwargs"]

        # For the gnn network, all the dimension are divided, except the input dims if pre-nn are missing
        if self.gnn is not None:
            factor_in_dim = self.pre_nn is not None
            kwargs["gnn_kwargs"] = self.gnn.make_mup_base_kwargs(
                divide_factor=divide_factor,
                factor_in_dim=factor_in_dim,
            )

        if self.finetuning_head is not None:
            kwargs["finetuning_head_kwargs"] = self.finetuning_head.make_mup_base_kwargs(
                divide_factor=divide_factor,
                factor_in_dim=True
            )

        return kwargs

    def set_max_num_nodes_edges_per_graph(self, max_nodes: Optional[int], max_edges: Optional[int]) -> None:
        """
        Set the maximum number of nodes and edges for all gnn layers and encoder layers

        Parameters:
            max_nodes: Maximum number of nodes in the dataset.
                This will be useful for certain architecture, but ignored by others.

            max_edges: Maximum number of edges in the dataset.
                This will be useful for certain architecture, but ignored by others.
        """
        self.max_num_nodes_per_graph = max_nodes
        self.max_num_edges_per_graph = max_edges
        if (self.encoder_manager is not None) and (self.encoder_manager.pe_encoders is not None):
            for encoder in self.encoder_manager.pe_encoders.values():
                encoder.max_num_nodes_per_graph = max_nodes
                encoder.max_num_edges_per_graph = max_edges
        if self.gnn is not None:
            for layer in self.gnn.layers:
                if isinstance(layer, BaseGraphStructure):
                    layer.max_num_nodes_per_graph = max_nodes
                    layer.max_num_edges_per_graph = max_edges

        self.task_heads.set_max_num_nodes_edges_per_graph(max_nodes, max_edges)

    def overwrite_with_pretrained(self, cfg, pretrained_model):
        cfg_finetune = cfg["finetuning"]
        task_head_from_pretrained = cfg_finetune["task_head_from_pretrained"]
        task = cfg_finetune["task"]
        added_depth = cfg_finetune["added_depth"]

        for module in ["pre_nn", "pre_nn_edges", "gnn", "graph_output_nn", "task_heads"]:
            if module == cfg_finetune["module_from_pretrained"]:
                break

            self.overwrite_complete_module(module, pretrained_model)

        self.overwrite_partial_module(module, task, task_head_from_pretrained, added_depth, pretrained_model)

    def overwrite_partial_module(
        self, module, task, task_head_from_pretrained, added_depth, pretrained_model
    ):
        """Completely overwrite the specified module"""
        if module == "gnn":
            shared_depth = len(self.task_heads.task_heads[task].layers) - added_depth
            assert shared_depth >= 0
            if shared_depth > 0:
                self.gnn.layers[:shared_depth] = pretrained_model.gnn.layers[:shared_depth]

        elif module == "graph_output_nn":
            for task_level in self.task_heads.graph_output_nn.keys():
                shared_depth = len(self.task_heads.graph_output_nn[task_level].graph_output_nn.layers) - added_depth
                assert shared_depth >= 0
                if shared_depth > 0:
                    self.task_heads.graph_output_nn[task_level].graph_output_nn.layers = (
                        pretrained_model.task_heads.graph_output_nn[task_level].graph_output_nn.layers[:shared_depth]
                        + self.task_heads.graph_output_nn[task_level].graph_output_nn.layers[shared_depth:]
                    )

        elif module == "task_heads":
            shared_depth = len(self.task_heads.task_heads[task].layers) - added_depth
            assert shared_depth >= 0
            if shared_depth > 0:
                self.task_heads.task_heads[task].layers = (
                    pretrained_model.task_heads.task_heads[task_head_from_pretrained].layers[:shared_depth]
                    + self.task_heads.task_heads[task].layers[shared_depth:]
                )

        elif module in ["pre_nn", "pre_nn_edges"]:
            raise NotImplementedError(f"Finetune from (edge) pre-NNs is not supported")

        else:
            raise NotImplementedError(f"This is an unknown module type")

    def overwrite_complete_module(self, module, pretrained_model):
        """Completely overwrite the specified module"""
        if module == "pre_nn":
            try:
                self.pre_nn.layers = pretrained_model.pre_nn.layers
            except:
                logger.warning(
                    f"Pretrained ({pretrained_model.pre_nn}) and/or finetune model ({self.pre_nn}) do not use a pre-NN."
                )

        elif module == "pre_nn_edges":
            try:
                self.pre_nn_edges.layers = pretrained_model.pre_nn_edges.layers
            except:
                logger.warning(
                    f"Pretrained ({pretrained_model.pre_nn_edges}) and/or finetune model ({self.pre_nn_edges}) do not use a pre-NN-edges."
                )

        elif module == "gnn":
            self.gnn.layers = pretrained_model.gnn.layers

        elif module == "graph_output_nn":
            for task_level in self.task_heads.graph_output_nn.keys():
                self.task_heads.graph_output_nn[task_level] = pretrained_model.task_heads.graph_output_nn[
                    task_level
                ]

        else:
            raise NotImplementedError(f"This is an unknown module type")

    @property
    def in_dim(self) -> int:
        r"""
        Returns the input dimension of the network
        """
        if self.pre_nn is not None:
            return self.pre_nn.in_dim
        else:
            return self.gnn.in_dim

    @property
    def out_dim(self) -> int:
        r"""
        Returns the output dimension of the network
        """
        return self.gnn.out_dim

    @property
    def out_dim_edges(self) -> int:
        r"""
        Returns the output dimension of the edges
        of the network.
        """
        if self.gnn.full_dims_edges is not None:
            return self.gnn.full_dims_edges[-1]
        return self.gnn.in_dim_edges

    @property
    def in_dim_edges(self) -> int:
        r"""
        Returns the input edge dimension of the network
        """
        return self.gnn.in_dim_edges
    

class FinetuningHead(nn.Module, MupMixin):
    def __init__(
        self,
        finetuning_head_kwargs: Dict[str, Any]
    ):
        r"""
        A flexible neural network architecture, with variable hidden dimensions,
        support for multiple layer types, and support for different residual
        connections.

        This class is meant to work with different graph neural networks
        layers. Any layer must inherit from `graphium.nn.base_graph_layer.BaseGraphStructure`
        or `graphium.nn.base_graph_layer.BaseGraphLayer`.

        Parameters:

            ...

        """

        super().__init__()
        self.task = finetuning_head_kwargs.pop("task", None)
        self.previous_module = finetuning_head_kwargs.pop("previous_module", "task_heads")
        self.incoming_level = finetuning_head_kwargs.pop("incoming_level", "graph")

        model_type = finetuning_head_kwargs.pop("model_type", "mlp")
        finetuning_model = FINETUNING_HEAD_DICT[model_type]
        self.finetuning_head = finetuning_model(**finetuning_head_kwargs)

    def forward(self, g: Union[torch.Tensor, Batch]):

        if self.previous_module == "task_heads":
            g = list(g.values())[0]

        g = self.finetuning_head.forward(g)

        return {self.task: g}

    def make_mup_base_kwargs(self, divide_factor: float = 2.0, factor_in_dim: bool = False) -> Dict[str, Any]:
        """
        Create a 'base' model to be used by the `mup` or `muTransfer` scaling of the model.
        The base model is usually identical to the regular model, but with the
        layers width divided by a given factor (2 by default)

        Parameter:
            divide_factor: Factor by which to divide the width.
            factor_in_dim: Whether to factor the input dimension

        Returns:
            Dictionary with the kwargs to create the base model.
        """
        # For the post-nn network, all the dimension are divided

        return self.finetuning_head.make_mup_base_kwargs(divide_factor=divide_factor, factor_in_dim=factor_in_dim)
from typing import Dict, List, Tuple, Optional, Union, Callable

import torch
from torch import Tensor
from torch.nn import Linear
from torch_scatter import scatter

from torch_geometric.nn.conv import MessagePassing
from torch_geometric.typing import OptTensor
from torch_geometric.utils import degree

from goli.utils.decorators import classproperty
from goli.nn.base_layers import MLP, get_activation
from goli.nn.base_graph_layer import BaseGraphLayer

class PNAMessagePassingPyg(MessagePassing, BaseGraphLayer):
    r"""
    Implementation of the message passing architecture of the PNA message passing layer,
    previously known as `PNALayerComplex`. This layer applies an MLP as
    pretransformation to the concatenation of $[h_u, h_v, e_{uv}]$ to generate
    the messages, with $h_u$ the node feature, $h_v$ the neighbour node features,
    and $e_{uv}$ the edge feature between the nodes $u$ and $v$.

    After the pre-transformation, it aggregates the messages
    multiple aggregators and scalers,
    concatenates their results, then applies an MLP on the concatenated
    features.

    PNA: Principal Neighbourhood Aggregation
    Gabriele Corso, Luca Cavalleri, Dominique Beaini, Pietro Lio, Petar Velickovic
    https://arxiv.org/abs/2004.05718

    [!] code adapted from pytorch-geometric implementation of PNAConv
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        aggregators: List[str],
        scalers: List[str],
        activation: Union[Callable, str] = "relu",
        dropout: float = 0.0,
        normalization: Union[str, Callable] = "none",
        avg_d: Dict[str, float] = {"log": 1.0},
        last_activation: Union[Callable, str] = "none",
        posttrans_layers: int = 1,
        pretrans_layers: int = 1,
        in_dim_edges: int = 0,
    ):
        r"""

        Parameters:

            in_dim:
                Input feature dimensions of the layer

            out_dim:
                Output feature dimensions of the layer

            aggregators:
                Set of aggregation function identifiers,
                e.g. "mean", "max", "min", "std", "sum", "var", "moment3".
                The results from all aggregators will be concatenated.

            scalers:
                Set of scaling functions identifiers
                e.g. "identidy", "amplification", "attenuation"
                The results from all scalers will be concatenated

            activation:
                activation function to use in the layer

            dropout:
                The ratio of units to dropout. Must be between 0 and 1

            normalization:
                Normalization to use. Choices:

                - "none" or `None`: No normalization
                - "batch_norm": Batch normalization
                - "layer_norm": Layer normalization
                - `Callable`: Any callable function

            avg_d:
                Average degree of nodes in the training set, used by scalers to normalize

            last_activation:
                activation function to use in the last layer of the internal MLP

            posttrans_layers:
                number of layers in the MLP transformation after the aggregation

            pretrans_layers:
                number of layers in the transformation before the aggregation

            in_dim_edges:
                size of the edge features. If 0, edges are ignored

        """

        super(BaseGraphLayer, self).__init__(
            in_dim=in_dim,
            out_dim=out_dim,
            activation=activation,
            dropout=dropout,
            normalization=normalization,
        )
        super().__init__(node_dim=0)

        self.aggregators = aggregators
        self.scalers = scalers
        self.in_dim_edges = in_dim_edges

        # Edge dimensions
        self.in_dim_edges = in_dim_edges
        if self.in_dim_edges > 0:
            self.edge_encoder = Linear(self.in_dim_edges, self.F_in)

        # Initializing basic attributes
        self.avg_d = avg_d
        self.last_activation = get_activation(last_activation)

        # Initializing aggregators and scalers
        self.aggregators = self.parse_aggregators(aggregators)
        self.scalers = self._parse_scalers(scalers)

        # MLP used on each pair of nodes with their edge MLP(h_u, h_v, e_uv)
        self.pretrans = MLP(
            in_dim=2 * in_dim + in_dim_edges,
            hidden_dim=in_dim,
            out_dim=in_dim,
            layers=pretrans_layers,
            activation=self.activation,
            last_activation=self.last_activation,
            dropout=dropout,
            normalization=normalization,
            last_normalization=normalization,
        )

        # MLP used on the aggregated messages of the neighbours
        self.posttrans = MLP(
            in_dim=(len(aggregators) * len(scalers)) * (self.in_dim + self.in_dim_edges),
            hidden_dim=self.out_dim,
            out_dim=self.out_dim,
            layers=posttrans_layers,
            activation=self.activation,
            last_activation=self.last_activation,
            dropout=dropout,
            normalization=normalization,
            last_normalization=normalization,
        )

    def forward(self, batch):
        x, edge_index, edge_attr = batch.x, batch.edge_index, batch.edge_attr

        out = self.propagate(edge_index, x=x, edge_attr=edge_attr, size=None)
        out = self.posttrans(out) # No more towers and concat with x
        return batch

    def message(self, x_i: Tensor, x_j: Tensor,
                edge_attr: OptTensor) -> Tensor:

        h: Tensor = x_i  # Dummy.
        if edge_attr is not None:
            edge_attr = self.edge_encoder(edge_attr)
            edge_attr = edge_attr.view(-1, 1, self.F_in)
            edge_attr = edge_attr.repeat(1, self.towers, 1)
            h = torch.cat([x_i, x_j, edge_attr], dim=-1)
        else:
            h = torch.cat([x_i, x_j], dim=-1)

        return self.pretrans(h) # No more towers

    def aggregate(self, inputs: Tensor, index: Tensor,
                  dim_size: Optional[int] = None) -> Tensor:

        outs = []
        for aggregator in self.aggregators:
            if aggregator == 'sum':
                out = scatter(inputs, index, 0, None, dim_size, reduce='sum')
            elif aggregator == 'mean':
                out = scatter(inputs, index, 0, None, dim_size, reduce='mean')
            elif aggregator == 'min':
                out = scatter(inputs, index, 0, None, dim_size, reduce='min')
            elif aggregator == 'max':
                out = scatter(inputs, index, 0, None, dim_size, reduce='max')
            elif aggregator == 'var' or aggregator == 'std':
                mean = scatter(inputs, index, 0, None, dim_size, reduce='mean')
                mean_squares = scatter(inputs * inputs, index, 0, None,
                                       dim_size, reduce='mean')
                out = mean_squares - mean * mean
                if aggregator == 'std':
                    out = torch.sqrt(torch.relu(out) + 1e-5)
            else:
                raise ValueError(f'Unknown aggregator "{aggregator}".')
            outs.append(out)
        out = torch.cat(outs, dim=-1)

        deg = degree(index, dim_size, dtype=inputs.dtype)
        deg = deg.clamp_(1).view(-1, 1, 1)

        outs = []
        for scaler in self.scalers:
            if scaler == 'identity':
                pass
            elif scaler == 'amplification':
                out = out * (torch.log(deg + 1) / self.avg_deg['log'])
            elif scaler == 'attenuation':
                out = out * (self.avg_deg['log'] / torch.log(deg + 1))
            elif scaler == 'linear':
                out = out * (deg / self.avg_deg['lin'])
            elif scaler == 'inverse_linear':
                out = out * (self.avg_deg['lin'] / deg)
            else:
                raise ValueError(f'Unknown scaler "{scaler}".')
            outs.append(out)
        return torch.cat(outs, dim=-1)

    @property
    def layer_outputs_edges(self) -> bool:
        r"""
        Abstract method. Return a boolean specifying if the layer type
        uses edges as input or not.
        It is different from ``layer_supports_edges`` since a layer that
        supports edges can decide to not use them.

        Returns:

            bool:
                Always ``False`` for the current class
        """
        return False

    @property
    def out_dim_factor(self) -> int:
        r"""
        Get the factor by which the output dimension is multiplied for
        the next layer.

        For standard layers, this will return ``1``.

        But for others, such as ``GatLayer``, the output is the concatenation
        of the outputs from each head, so the out_dim gets multiplied by
        the number of heads, and this function should return the number
        of heads.

        Returns:

            int:
                Always ``1`` for the current class
        """
        return 1

    @classproperty
    def layer_supports_edges(cls) -> bool:
        r"""
        Return a boolean specifying if the layer type supports edges or not.

        Returns:

            bool:
                Always ``True`` for the current class
        """
        return True

    @property
    def layer_inputs_edges(self) -> bool:
        r"""
        Return a boolean specifying if the layer type
        uses edges as input or not.
        It is different from ``layer_supports_edges`` since a layer that
        supports edges can decide to not use them.

        Returns:

            bool:
                Returns ``self.edge_features``
        """
        return self.edge_features

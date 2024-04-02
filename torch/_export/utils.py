import dataclasses
import inspect
import math
import operator
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type

import torch
from torch._subclasses.fake_tensor import FakeTensor

from torch.export import ExportedProgram
from torch.export.exported_program import _rename_without_collisions
from torch.export.graph_signature import ConstantArgument, InputKind, OutputKind
from torch.utils._pytree import (
    _register_pytree_node,
    Context,
    FlattenFunc,
    FromDumpableContextFn,
    GetAttrKey,
    KeyPath,
    keystr,
    MappingKey,
    SequenceKey,
    ToDumpableContextFn,
    tree_flatten_with_path,
    UnflattenFunc,
)


def _check_input_constraints_for_graph(
    input_placeholders: List[torch.fx.Node], flat_args_with_path, range_constraints
):
    def get_keystr(key_path: KeyPath) -> str:
        """For a given index into the flat_args, return a human readable string
        describing how to access it, e.g. "*args["foo"][0].bar"
        """
        # Prefix the keypath with "*args" or "**kwargs" to make it clearer where
        # the arguments come from. Ultimately we ought to serialize the
        # original arg names for the best error message here.
        args_kwargs_key_path = key_path[0]
        assert isinstance(args_kwargs_key_path, SequenceKey)
        if args_kwargs_key_path.idx == 0:
            return f"*args{keystr(key_path[1:])}"
        else:
            kwarg_key = key_path[1]
            assert isinstance(kwarg_key, MappingKey)
            name = str(kwarg_key)[1:-1]  # get rid of the enclosed []
            return f"{name}{keystr(key_path[2:])}"

    import sympy

    from torch._export.passes.add_runtime_assertions_for_constraints_pass import (
        _convert_range_to_int,
    )
    from torch.utils._sympy.solve import try_solve

    if len(flat_args_with_path) != len(input_placeholders):
        raise RuntimeError(
            "Unexpected number of inputs "
            f"(expected {len(input_placeholders)}, got {len(flat_args_with_path)})"
        )
    # NOTE: export already guarantees that the same symbol is used in metadata
    # for all InputDims related by equality constraints, so we can just unify
    # symbols with given input dimension values to check equality constraints.
    unification_map: "Dict[sympy.Symbol, Any]" = {}
    for (key_path, arg), node in zip(flat_args_with_path, input_placeholders):
        node_val = node.meta.get("val")
        if isinstance(node_val, FakeTensor):
            if not isinstance(arg, torch.Tensor):
                raise RuntimeError(
                    f"Expected input at {get_keystr(key_path)} to be a tensor, but got {type(arg)}",
                )

            if len(node_val.shape) != len(arg.shape):
                raise RuntimeError(
                    f"Unexpected number of dimensions in input at {get_keystr(key_path)}.shape "
                    f"(expected {node_val.shape}, got {arg.shape})"
                )

            for j, (arg_dim, node_dim) in enumerate(zip(arg.shape, node_val.shape)):
                # TODO(avik): Assert the following property in the IR verifier:
                # node_dim is either an int or a SymInt containing an int or a unary sympy.Expr
                if (
                    isinstance(node_dim, torch.SymInt)
                    and len(node_dim.node.expr.free_symbols) == 1
                ):
                    symbol = next(iter(node_dim.node.expr.free_symbols))
                    if symbol in unification_map:
                        existing_dim = node_dim.node.expr.subs(unification_map)
                        if arg_dim != existing_dim:
                            raise RuntimeError(
                                f"Expected input at {get_keystr(key_path)}.shape[{j}] to be equal to "
                                f"{existing_dim}, but got {arg_dim}",
                            )
                    else:
                        if (
                            isinstance(arg_dim, torch.SymInt)
                            and not arg_dim.node.expr.is_number
                        ):
                            # This can happen when, say, arg is a fake tensor.
                            # We do not run checks on symbolic shapes of fake inputs as
                            # such checks can affect the shape env.
                            pass
                        else:
                            solution = try_solve(
                                sympy.Eq(node_dim.node.expr, arg_dim), symbol
                            )
                            if solution is None:
                                raise RuntimeError(  # noqa: TRY200
                                    f"Expected input {node.name}.shape[{j}] = {arg_dim} to be "
                                    f"of the form {node_dim.node.expr}, where {symbol} is an integer"
                                )
                            else:
                                unification_map[symbol] = int(solution[1])

                    if node_dim.node.expr in range_constraints:
                        min_val, max_val = _convert_range_to_int(
                            range_constraints[node_dim.node.expr]
                        )
                        # NOTE: we allow dimensions to be 0/1 at runtime
                        if min_val > 2:
                            if arg_dim < min_val:
                                raise RuntimeError(
                                    f"Expected input at {get_keystr(key_path)}.shape[{j}] to be >= "
                                    f"{min_val}, but got {arg_dim}",
                                )
                        if max_val < math.inf:
                            if arg_dim > max_val:
                                raise RuntimeError(
                                    f"Expected input at {get_keystr(key_path)}.shape[{j}] to be <= "
                                    f"{max_val}, but got {arg_dim}",
                                )
                else:
                    if arg_dim != node_dim:
                        raise RuntimeError(
                            f"Expected input at {get_keystr(key_path)}.shape[{j}] to be equal to "
                            f"{node_dim}, but got {arg_dim}",
                        )
        elif isinstance(node_val, (int, float, str)):
            if type(arg) != type(node_val) or arg != node_val:
                raise RuntimeError(
                    f"Expected input at {get_keystr(key_path)} to be equal to {node_val}, but got {arg}",
                )


def register_dataclass_as_pytree_node(
    cls: Type[Any],
    flatten_fn: Optional[FlattenFunc] = None,
    unflatten_fn: Optional[UnflattenFunc] = None,
    *,
    serialized_type_name: Optional[str] = None,
    to_dumpable_context: Optional[ToDumpableContextFn] = None,
    from_dumpable_context: Optional[FromDumpableContextFn] = None,
    return_none_fields: bool = False,
) -> None:
    assert dataclasses.is_dataclass(
        cls
    ), f"Only dataclasses can be registered with this function: {cls}"

    def default_flatten_fn(obj: Any) -> Tuple[List[Any], Context]:
        flattened = []
        flat_names = []
        none_names = []
        for f in dataclasses.fields(obj):
            name, val = f.name, getattr(obj, f.name)
            if val is not None or return_none_fields:
                flattened.append(val)
                flat_names.append(name)
            else:
                none_names.append(name)
        return flattened, [flat_names, none_names]

    def default_unflatten_fn(values: Iterable[Any], context: Context) -> Any:
        flat_names, none_names = context
        return cls(**dict(zip(flat_names, values)), **dict.fromkeys(none_names))

    def default_flatten_fn_with_keys(obj: Any) -> Tuple[List[Any], Context]:
        flattened, (flat_names, none_names) = flatten_fn(obj)  # type: ignore[misc]
        return [(MappingKey(k), v) for k, v in zip(flat_names, flattened)], flat_names

    flatten_fn = flatten_fn if flatten_fn is not None else default_flatten_fn
    unflatten_fn = unflatten_fn if unflatten_fn is not None else default_unflatten_fn

    if (to_dumpable_context is None) ^ (from_dumpable_context is None):
        raise ValueError(
            f"Both to_dumpable_context and from_dumpable_context for {cls} must "
            "be None or registered."
        )

    _register_pytree_node(
        cls,
        flatten_fn,
        unflatten_fn,
        serialized_type_name=serialized_type_name,
        flatten_with_keys_fn=default_flatten_fn_with_keys,
        to_dumpable_context=to_dumpable_context,
        from_dumpable_context=from_dumpable_context,
    )


def is_param(program: ExportedProgram, node: torch.fx.Node) -> bool:
    """
    Checks if the given node is a parameter within the exported program
    """

    return node.name in program.graph_signature.inputs_to_parameters


def get_param(
    program: ExportedProgram,
    node: torch.fx.Node,
) -> Optional[torch.nn.Parameter]:
    """
    Returns the parameter associated with the given node in the exported program.
    Returns None if the node is not a parameter within the exported program
    """

    if is_param(program, node):
        parameter_name = program.graph_signature.inputs_to_parameters[node.name]
        return program.state_dict[parameter_name]

    return None


def is_buffer(program: ExportedProgram, node: torch.fx.Node) -> bool:
    """
    Checks if the given node is a buffer within the exported program
    """

    return node.name in program.graph_signature.inputs_to_buffers


def get_buffer(
    program: ExportedProgram,
    node: torch.fx.Node,
) -> Optional[torch.Tensor]:
    """
    Returns the buffer associated with the given node in the exported program.
    Returns None if the node is not a buffer within the exported program
    """

    if is_buffer(program, node):
        buffer_name = program.graph_signature.inputs_to_buffers[node.name]
        if buffer_name in program.graph_signature.non_persistent_buffers:
            return program.constants[buffer_name]
        else:
            return program.state_dict[buffer_name]

    return None


def is_lifted_tensor_constant(
    program: ExportedProgram,
    node: torch.fx.Node,
) -> bool:
    """
    Checks if the given node is a lifted tensor constant within the exported program
    """

    return node.name in program.graph_signature.inputs_to_lifted_tensor_constants


def get_lifted_tensor_constant(
    program: ExportedProgram,
    node: torch.fx.Node,
) -> Optional[torch.Tensor]:
    """
    Returns the lifted tensor constant associated with the given node in the exported program.
    Returns None if the node is not a lifted tensor constant within the exported program
    """

    if is_lifted_tensor_constant(program, node):
        lifted_tensor_name = program.graph_signature.inputs_to_lifted_tensor_constants[
            node.name
        ]
        return program.constants[lifted_tensor_name]

    return None


def sequential_split(gm: torch.fx.GraphModule, node_call_back) -> torch.fx.GraphModule:
    """
    Splits the graph module into multiple submodules based on the node_call_back.
    The node_call_back should return True if the node is a delimiter. Delimiter will be
    the first node in the next submodule.
    """
    from torch.fx.passes.split_module import split_module

    split_map = {}
    split_id = 0
    for node in gm.graph.nodes:
        if node_call_back(node):
            split_id += 1
        split_map[node] = split_id

    new_gm = split_module(
        gm,
        gm,
        lambda node: split_map[node],
        keep_original_order=True,
        keep_original_node_name=True,
    )
    # Keep the codegen from original graph module to preserve e.g. pytree info.
    new_gm.graph._codegen = gm.graph._codegen
    new_gm.recompile()
    return new_gm


def nodes_filter(nodes: List[torch.fx.Node], node_call_back) -> List[torch.fx.Node]:
    """Returns the nodes that match the node_call_back as a list."""
    return [node for node in nodes if node_call_back(node)]


def nodes_first(
    nodes: List[torch.fx.Node], node_call_back=None
) -> Optional[torch.fx.Node]:
    """
    Returns the first node that matches the node_call_back. If no node matches, returns None.
    When node_call_back is None, returns the first node in the node list.
    """
    ret = nodes_filter(nodes, node_call_back if node_call_back else lambda node: True)
    if len(ret) > 0:
        return ret[0]
    return None


def nodes_count(nodes: List[torch.fx.Node], node_call_back) -> int:
    """Returns the number of nodes that match the node_call_back."""
    return len(nodes_filter(nodes, node_call_back))


def nodes_map(nodes: List[torch.fx.Node], node_call_back) -> List[torch.fx.Node]:
    """
    Sequentially visit the nodes list and invoke node_call_back on each element.
    Returns the nodes list after the node_call_back is invoked on each element.
    """
    for node in nodes:
        node_call_back(node)
    return nodes


def node_replace_(
    old_node: torch.fx.Node, new_node: torch.fx.Node, delete_old: bool = False
) -> None:
    """
    Replace all uses of old_node with new_node.
    """
    old_node.replace_all_uses_with(new_node)
    if delete_old:
        old_node.users.clear()
        old_node.graph.erase_node(old_node)


def node_inline_(call_mod_node: torch.fx.Node) -> None:
    """
    Inline the submodule of the given node into the parent module.
    Note: we only support the case where submodule takes tensors inputs.
    """
    assert call_mod_node.op == "call_module"
    gm = call_mod_node.graph.owning_module

    assert isinstance(call_mod_node.target, str)
    sub_gm = getattr(gm, call_mod_node.target)

    phs = (node for node in sub_gm.graph.nodes if node.op == "placeholder")
    body = (
        node for node in sub_gm.graph.nodes if node.op not in ("placeholder", "output")
    )
    output = [node for node in sub_gm.graph.nodes if node.op == "output"]

    for ph, arg in zip(phs, call_mod_node.args):
        assert isinstance(arg, torch.fx.Node)
        node_replace_(ph, arg, delete_old=True)

    with gm.graph.inserting_before(call_mod_node):
        for node in body:
            new_node = gm.graph.node_copy(node)
            node_replace_(node, new_node, delete_old=True)

        if len(output) > 0:
            assert len(output) == 1 and len(output[0].args) == 1
            new_output = output[0].args[0]

            if isinstance(new_output, torch.fx.Node):
                node_replace_(call_mod_node, new_output, delete_old=True)
            elif isinstance(new_output, (list, tuple)):
                # Inline the get_item calls for the output node.
                get_item_users = nodes_filter(
                    list(call_mod_node.users.keys()),
                    lambda node: node.op == "call_function"
                    and node.target == operator.getitem,
                )
                # get_item_node.args[1] is the idx referring to new_output[idx]
                nodes_map(
                    get_item_users,
                    lambda get_item_node: node_replace_(
                        get_item_node,
                        new_output[get_item_node.args[1]],
                        delete_old=True,
                    ),
                )
                call_mod_node.graph.erase_node(call_mod_node)
            else:
                raise NotImplementedError(
                    f"Unsupported output type {type(new_output)}. Expect it to be a Node or a list/tuple of Nodes."
                )
        else:
            call_mod_node.graph.erase_node(call_mod_node)

    gm.delete_all_unused_submodules()
    gm.recompile()
    return gm


def placeholder_naming_pass(
    gm: torch.fx.GraphModule,
    export_graph_signature: torch.export.ExportGraphSignature,
    mod: torch.nn.Module,
    fake_args,
    fake_kwargs,
    fake_params_buffers,
    constants: Dict[str, Any],
) -> None:
    """
    This pass is run at the end of _export_non_strict() to assign better placeholder node names:
        - User inputs:
            These follow the signature of mod.forward(), e.g. forward(x, y) produces nodes x, y.
            For nested inputs from dictionaries, lists, tuples, or dataclasses,
            the names are a concatenation of the path to the tensor.
                e.g. x = {
                    'a': torch.randn(),
                    'b': [torch.randn(), torch.randn()]
                }
            produces nodes x_a, x_b_0, x_b_1.
        - Parameters/buffers/constants/custom objects:
            These follow the FQN of the object, prefixed by "p", "b", "c", "obj" respectively.
                e.g. self.bar.l0.weight produces "p_bar_l0_weight".
        - Effect tokens:
            These are named token, token_1, ...
    """

    prefixes = {
        InputKind.PARAMETER: "p",
        InputKind.BUFFER: "b",
        InputKind.CONSTANT_TENSOR: "c",
        InputKind.CUSTOM_OBJ: "obj",
    }

    def _strip_name(x):
        if x.startswith("L__self___"):
            x = x[len("L__self___") :]
        return x.replace(".", "_")

    def _extract_pytree_key(x):
        if isinstance(x, MappingKey):
            return str(x.key).replace(".", "_")
        elif isinstance(x, SequenceKey):
            return str(x.idx)
        elif isinstance(x, GetAttrKey):
            return x.name
        else:
            raise RuntimeError(f"Pytree key of type {type(x)} not handled for {x}")

    name_map: Dict[str, str] = {}

    # map user input names with mod.forward() signature
    combined_args = (
        inspect.signature(mod.forward).bind(*fake_args, **fake_kwargs).arguments
    )
    flat_args_with_path, _ = tree_flatten_with_path(combined_args)
    user_input_names = [
        (None if isinstance(spec.arg, ConstantArgument) else spec.arg.name)
        for spec in export_graph_signature.input_specs
        if spec.kind == InputKind.USER_INPUT
    ]

    # use pytree path to name nested user inputs
    for (arg_path, arg), user_input_name in zip(flat_args_with_path, user_input_names):
        if user_input_name:
            _rename_without_collisions(
                name_map,
                user_input_name,
                "_".join(_extract_pytree_key(x).lower() for x in arg_path),
                is_placeholder=True,
            )

    # use graph signature input specs to map param/buffer/constant names
    # name effect tokens as token, token_1, ... (these aren't visible to user)
    for spec in export_graph_signature.input_specs:
        # this should never be ConstantArgument, but avoid lint issue
        if isinstance(spec.arg, ConstantArgument):
            continue
        if spec.kind in [
            InputKind.PARAMETER,
            InputKind.BUFFER,
            InputKind.CONSTANT_TENSOR,
            InputKind.CUSTOM_OBJ,
        ]:
            _rename_without_collisions(
                name_map,
                spec.arg.name,
                f"{prefixes[spec.kind]}_{_strip_name(spec.target).lower()}",
                is_placeholder=True,
            )
        elif spec.kind == InputKind.TOKEN:
            _rename_without_collisions(
                name_map, spec.arg.name, "token", is_placeholder=True
            )  # handle collisions

    # handle naming collisions with call_function/get_attr inputs.
    # here, we want to prioritize user input names over call_function names
    # e.g. not have forward(self, mul): lead to a placeholder node called mul_13,
    # so we increment the suffix of call_function nodes as needed
    for node in gm.graph.nodes:
        if node.op == "placeholder":
            continue
        _rename_without_collisions(name_map, node.name, node.name)

    # assign new node names
    for node in gm.graph.nodes:
        if node.op == "placeholder":
            if node.name in name_map:  # skip constant inputs
                node.name = node.target = name_map[node.name]
        elif node.name in name_map:
            node.name = name_map[node.name]

    # TODO(pianpwk), in immediate follow-up PR
    # propagate names to higher order op subgraphs
    # name_hoo_subgraph_placeholders(gm)

    # re-generate graph module code
    gm.recompile()

    # modify graph signature (input specs, output specs, user input mutations)
    for spec in export_graph_signature.input_specs:
        if isinstance(spec.arg, ConstantArgument):
            continue
        assert spec.arg.name in name_map
        spec.arg.name = name_map[spec.arg.name]
        if (  # handle targets for custom objects
            spec.kind == InputKind.CUSTOM_OBJ and spec.target in name_map
        ):
            spec.target = name_map[spec.target][4:]  # strip obj_ prefix

    for spec in export_graph_signature.output_specs:
        if isinstance(spec.arg, ConstantArgument):
            continue
        if spec.arg.name in name_map:
            spec.arg.name = name_map[spec.arg.name]
        if spec.kind == OutputKind.USER_INPUT_MUTATION and spec.target in name_map:
            spec.target = name_map[spec.target]

    # rename keys in constants dict for custom objects
    for name in list(constants.keys()):
        constant = constants[name]
        if name in name_map and not isinstance(
            constant, torch.Tensor
        ):  # rename custom objects
            new_name = name_map[name][4:]  # strip obj_ prefix
            if new_name != name:
                constants[new_name] = constant
                del constants[name]

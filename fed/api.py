import functools
import inspect
import logging
import queue
import threading
from typing import Any, Dict, List, Union
from fed._private.global_context import get_global_context

# from fed.fed_actor import FedActor

import jax
import ray
from ray._private.inspect_util import is_cython
from ray.dag import PARENT_CLASS_NODE_KEY, PREV_CLASS_METHOD_CALL_KEY
from ray.dag.class_node import ClassMethodNode, ClassNode, _UnboundClassMethodNode
from ray.dag.function_node import FunctionNode

from fed._private.fed_dag_node import (
    FedDAGClassNode,
    FedDAGClassMethodNode,
)
from fed.fed_object import FedObject
from .barriers import send_op, recv_op
from fed.utils import resolve_dependencies
from fed.cleanup import push_to_sending

logger = logging.getLogger(__file__)


_PARTY = None


def set_party(party: str):
    global _PARTY
    _PARTY = party


def get_party():
    global _PARTY
    return _PARTY


_CLUSTER = None
'''
{
    'alice': '127.0.0.1:10001',
    'bob': '127.0.0.1:10002',
}
'''


def get_cluster():
    global _CLUSTER
    return _CLUSTER


def set_cluster(cluster: Dict, party: str = None):
    global _CLUSTER
    _CLUSTER = cluster
    if party:
        set_party(party)


class FedDAGFunctionNode(FunctionNode):
    def __init__(self, func_body, func_args, func_kwargs, party: str):
        self._func_body = func_body
        self._party = party
        super().__init__(func_body, func_args, func_kwargs, None)

    def get_func_or_method_name(self):
        return self._func_body.__name__

    def get_party(self):
        return self._party


class FedRemoteFunction:
    def __init__(self, func_or_class) -> None:
        self._node_party = None
        self._func_body = func_or_class
        self._options = {}

    def party(self, party: str):
        self._node_party = party
        return self

    def options(self, **options):
        self._options = options
        return self

    def remote(self, *args, **kwargs):
        # Generate a new fed task id for this call.
        fed_task_id = get_global_context().next_seq_id()

        ####################################
        # This might duplicate.
        fed_object = None
        self._party = get_party()  # TODO(qwang): Refine this.
        print(
            f"======self._party={self._party}, node_party={self._node_party}, func={self._func_body}, options={self._options}"
        )
        if self._party == self._node_party:
            resolved_args, resolved_kwargs = resolve_dependencies(
                self._party, fed_task_id, *args, **kwargs
            )
            # TODO(qwang): Handle kwargs.
            ray_obj_ref = self._execute_impl(args=resolved_args, kwargs=resolved_kwargs)
            if isinstance(ray_obj_ref, list):
                return [
                    FedObject(self._node_party, fed_task_id, ref, i)
                    for i, ref in enumerate(ray_obj_ref)
                ]
            else:
                return FedObject(self._node_party, fed_task_id, ray_obj_ref)
        else:
            flattened_args, _ = jax.tree_util.tree_flatten((args, kwargs))
            for arg in flattened_args:
                # TODO(qwang): We still need to cosider kwargs and a deeply object_ref in this party.
                if isinstance(arg, FedObject) and arg.get_party() == self._party:
                    cluster = get_cluster()
                    print(
                        f'[{self._party}] =====insert send_op to {self._node_party}, arg task id {arg.get_fed_task_id()}, to task id {fed_task_id}'
                    )
                    send_op_ray_obj = ray.remote(send_op).remote(
                        self._party,
                        cluster[self._node_party],
                        arg.get_ray_object_ref(),
                        arg.get_fed_task_id(),
                        fed_task_id,
                    )
                    push_to_sending(send_op_ray_obj)
            if (
                self._options
                and 'num_returns' in self._options
                and self._options['num_returns'] > 1
            ):
                num_returns = self._options['num_returns']
                return [FedObject(self._node_party, fed_task_id, None, i) for i in range(num_returns)]
            else:
                return FedObject(self._node_party, fed_task_id, None)
        ####################################
        return fed_object

    def _execute_impl(self, args, kwargs):
        return (
            ray.remote(self._func_body).options(**self._options).remote(*args, **kwargs)
        )


class FedRemoteClass:
    def __init__(self, func_or_class) -> None:
        self._party = None
        self._cls = func_or_class
        self._options = {}

    def party(self, party: str):
        self._party = party
        return self

    def options(self, **options):
        self._options = options
        return self

    def remote(self, *args, **kwargs):
        fed_class_task_id = get_global_context().next_seq_id()
        fed_class_node = FedDAGClassNode(
            fed_class_task_id,
            get_cluster(),
            self._cls,
            get_party(),
            self._party,
            self._options,
            args,
            kwargs,
        )
        fed_class_node._execute_impl()  # TODO(qwang): We shouldn't use Node.execute(), we should use `remote`.
        return fed_class_node


# This is the decorator `@fed.remote`
def remote(*args, **kwargs):
    def _make_fed_remote(function_or_class, **options):
        if inspect.isfunction(function_or_class) or is_cython(function_or_class):
            return FedRemoteFunction(function_or_class).options(**options)

        if inspect.isclass(function_or_class):
            return FedRemoteClass(function_or_class).options(**options)

        raise TypeError(
            "The @fed.remote decorator must be applied to either a function or a class."
        )

    if len(args) == 1 and len(kwargs) == 0 and callable(args[0]):
        # This is the case where the decorator is just @fed.remote.
        return _make_fed_remote(args[0])
    assert len(args) == 0 and len(kwargs) > 0, "Remote args error."
    return functools.partial(_make_fed_remote, **kwargs)


def get(fed_objects: Union[FedObject, List[FedObject]]) -> Any:
    """
    Gets the real data of the given fed_object.

    If the object is located in current party, return it immediately,
    otherwise return it after receiving the real data from the located
    party.
    """

    # A fake fed_task_id for a `fed.get()` operator. This is useful
    # to help contruct the DAG within `fed.get`.
    fake_fed_task_id = get_global_context().next_seq_id()
    cluster = get_cluster()
    current_party = get_party()
    is_individual_id = isinstance(fed_objects, FedObject)
    if is_individual_id:
        fed_objects = [fed_objects]

    ray_refs = []
    for fed_object in fed_objects:
        if fed_object.get_party() == current_party:
            # The code path of the fed_object is in current party, so
            # need to boardcast the data of the fed_object to other parties,
            # and then return the real data of that.
            ray_object_ref = fed_object.get_ray_object_ref()
            assert ray_object_ref is not None
            for party_name, party_addr in cluster.items():
                if party_name == current_party:
                    continue
                else:
                    send_op_ray_obj = ray.remote(send_op).remote(
                        current_party,
                        party_addr,
                        ray_object_ref,
                        fed_object.get_fed_task_id(),
                        fake_fed_task_id,
                    )
                    push_to_sending(send_op_ray_obj)

            ray_refs.append(ray_object_ref)
        else:
            # This is the code path that the fed_object is not in current party.
            # So we should insert a `recv_op` as a barrier to receive the real
            # data from the location party of the fed_object.
            recv_op_obj = ray.remote(recv_op).remote(
                current_party, fed_object.get_fed_task_id(), fake_fed_task_id
            )
            ray_refs.append(recv_op_obj)

    values = ray.get(ray_refs)
    if is_individual_id:
        values = values[0]

    return values

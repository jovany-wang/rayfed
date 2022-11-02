from typing import Optional
from fed._private.global_context import get_global_context
import ray
import jax
from ray.dag import PARENT_CLASS_NODE_KEY, PREV_CLASS_METHOD_CALL_KEY
from fed.utils import resolve_dependencies
from fed.fed_object import FedObject
from fed.barriers import send_op
from fed.cleanup import push_to_sending


class FedDAGClassNode:
    def __init__(
        self,
        fed_class_task_id,
        cluster,
        cls,
        party,
        node_party,
        options,
        cls_args,
        cls_kwargs,
    ) -> None:
        self._fed_class_task_id = fed_class_task_id
        self._cluster = cluster
        self._body = cls
        self._party = party
        self._node_party = node_party
        self._options = options
        self._cls_args = cls_args
        self._cls_kwargs = cls_kwargs
        self._last_call: Optional["FedDAGClassMethodNode"] = None
        self._actor_handle = None
        self._other_args_to_resolve = None

    def __getattr__(self, method_name: str):
        # User trying to call .bind() without a bind class method
        if method_name == "remote" and "remote" not in dir(self._body):
            raise AttributeError(f".remote() cannot be used again on {type(self)} ")
        # Raise an error if the method is invalid.
        getattr(self._body, method_name)
        call_node = FedDAGClassMethodNode(
            self._cluster,
            self._party,
            self._node_party,
            self,
            method_name,
            self._other_args_to_resolve,
        ).options(**self._options)
        return call_node

    def _execute_impl(self, *args, **kwargs):
        """Executor of ClassNode by ray.remote()

        Args and kwargs are to match base class signature, but not in the
        implementation. All args and kwargs should be resolved and replaced
        with value in bound_args and bound_kwargs via bottom-up recursion when
        current node is executed.
        """
        if self._node_party == self._party:
            self._actor_handle = (
                ray.remote(self._body)
                .options(**self._options)
                .remote(*self._cls_args, **self._cls_kwargs)
            )

    def _execute_remote_method(self, method_name, options, args, kwargs):
        num_returns = 1
        if options and 'num_returns' in options:
            num_returns = options['num_returns']
        print(
            f"[{self._party}] Actor method call: {method_name}, num_returns: {num_returns}"
        )
        ray_object_ref = self._actor_handle._actor_method_call(
            method_name,
            args=args,
            kwargs=kwargs,
            name="",
            num_returns=num_returns,
            concurrency_group_name="",
        )
        return ray_object_ref


class FedDAGClassMethodNode:
    def __init__(
        self,
        cluster,
        party,
        node_party,
        fed_dag_cls_node,
        method_name,
        other_args_to_resolve,
    ) -> None:
        self._cluster = cluster
        self._party = party  # Current party
        self._node_party = node_party
        self._fed_dag_cls_node = fed_dag_cls_node
        self._method_name = method_name
        self._options = {}
        self._fed_task_id = None  # None if uninitialized
        self._parent_class_node: FedDAGClassNode = fed_dag_cls_node
        # self._parent_class_node: FedDAGClassNode = other_args_to_resolve.get(
        #     PARENT_CLASS_NODE_KEY
        # )

    def remote(self, *args, **kwargs) -> FedObject:
        other_args_to_resolve = {
            PARENT_CLASS_NODE_KEY: self._fed_dag_cls_node,
            PREV_CLASS_METHOD_CALL_KEY: self._fed_dag_cls_node._last_call,
        }
        self._fed_dag_cls_node.last_call = self
        assert self._fed_task_id is None, ".remote() shouldn't be invoked twice."
        self._fed_task_id = get_global_context().next_seq_id()
        print(
            f"[{self._party}] next_seq_id={self._fed_task_id} for method_name={self._method_name}"
        )
        ####################################
        # This might duplicate.
        if self._party == self._node_party:
            print(
                f"[{self._party}] ##########################, method_name={self._method_name}"
            )
            resolved_args, resolved_kwargs = resolve_dependencies(
                self._party, self._fed_task_id, *args, **kwargs
            )
            print(
                f"[{self._party}] ##########################,  method_name={self._method_name}"
            )
            # TODO(qwang): Handle kwargs.
            print(
                f"[{self._party}] all dependencies={resolved_args}, {resolved_kwargs}"
            )
            ray_obj_ref = self._execute_impl(args=resolved_args, kwargs=resolved_kwargs)
            if isinstance(ray_obj_ref, list):
                return [
                    FedObject(self._node_party, self._fed_task_id, ref, i)
                    for i, ref in enumerate(ray_obj_ref)
                ]
            else:
                return FedObject(self._node_party, self._fed_task_id, ray_obj_ref)
        else:
            flattened_args, _ = jax.tree_util.tree_flatten((args, kwargs))
            for arg in flattened_args:
                # TODO(qwang): We still need to cosider kwargs and a deeply object_ref in this party.
                if isinstance(arg, FedObject) and arg.get_party() == self._party:
                    cluster = self._cluster
                    print(
                        f'[{self._party}] =====insert send_op to {self._node_party}, arg task id {arg.get_fed_task_id()}, to task id {self._fed_task_id}'
                    )
                    send_op_ray_obj = ray.remote(send_op).remote(
                        self._party,
                        cluster[self._node_party],
                        arg.get_ray_object_ref(),
                        arg.get_fed_task_id(),
                        self._fed_task_id,
                    )
                    push_to_sending(send_op_ray_obj)
            if (
                self._options
                and 'num_returns' in self._options
                and self._options['num_returns'] > 1
            ):
                num_returns = self._options['num_returns']
                return [
                    FedObject(self._node_party, self._fed_task_id, None, i)
                    for i in range(num_returns)
                ]
            else:
                return FedObject(self._node_party, self._fed_task_id, None)

    def options(self, **options):
        self._options = options
        return self

    def _execute_impl(self, args, kwargs):
        method_body = getattr(self._parent_class_node, self._method_name)
        # Execute with bound args.
        # return method_body.options(**self._bound_options).remote(
        #     *self._bound_args,
        #     **self._bound_kwargs,
        # )
        return self._fed_dag_cls_node._execute_remote_method(
            self._method_name, self._options, args, kwargs
        )


import multiprocessing
from sqlite3 import paramstyle
from tkinter import E

import pytest
import fed
import ray
from fed.api import set_cluster, set_party
from fed.barriers import start_recv_proxy


@fed.remote
class MyModel:
    def __init__(self, party, step_length):
        self._trained_steps = 0
        self._step_length = step_length
        self._weights = 0
        self._party = party

    def train(self):
        self._trained_steps += 1
        self._weights += self._step_length
        return self._weights

    def get_weights(self):
        return self._weights
    
    def set_weights(self, new_weights):
        self._weights = new_weights
        return new_weights

@fed.remote
def mean(x, y):
    return (x + y) / 2

cluster = {'alice': '127.0.0.1:11010', 'bob': '127.0.0.1:11011'}


def run(party):
    set_cluster(cluster=cluster)
    set_party(party)
    start_recv_proxy(cluster[party], party)

    epochs = 3
    alice_model = MyModel.party("alice").remote("alice", 2)
    bob_model = MyModel.party("bob").remote("bob", 4)

    all_mean_weights = []
    for epoch in range(epochs):
        w1 = alice_model.train.remote()
        w2 = bob_model.train.remote()
        new_weights = mean.party("alice").remote(w1, w2)
        result = fed.get(new_weights)
        alice_model.set_weights.remote(new_weights)
        bob_model.set_weights.remote(new_weights)
        all_mean_weights.append(result)
    assert all_mean_weights == [3, 6, 9]


def test_fed_get_in_2_parties():
    p_alice = multiprocessing.Process(target=run, args=('alice',))
    p_bob = multiprocessing.Process(target=run, args=('bob',))
    p_alice.start()
    p_bob.start()
    p_alice.join()
    p_bob.join()
    assert p_alice.exitcode == 0 and p_bob.exitcode == 0


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
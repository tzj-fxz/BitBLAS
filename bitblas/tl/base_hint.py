# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
from bitblas.base.roller.hint import Hint
from abc import ABC, abstractmethod
from typing import Dict


class BaseTLHint(ABC):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def __repr__(self):
        raise NotImplementedError("method __repr__ is not implemented")

    @classmethod
    def from_roller_hint(self, hint: Hint) -> 'BaseTLHint':
        raise NotImplementedError("method from_roller_hint is not implemented")

    @abstractmethod
    def get_config_params(self) -> Dict:
        pass

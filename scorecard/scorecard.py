from typing import Any, Dict, List, Optional, Union, Tuple
import copy

import numexpr as ne
import numpy as np
import pandas as pd
from scipy.sparse import hstack
from scipy.optimize import minimize

from .discretize import discretize
from .model import Model
from .performance import Performance
from .variable import Variable

EvalSets = Optional[List[Tuple[pd.DataFrame, Performance]]]


class Scorecard:
    @classmethod
    def discretize(
        cls,
        df: pd.DataFrame,
        perf: Performance,
        missing: float = np.nan,
        exceptions: Optional[Union[Dict[str, List[Any]], List[Any]]] = None,
        keep_data: bool = True,
        **kwargs,
    ):
        variables = discretize(df, perf, missing, exceptions, **kwargs)
        if keep_data:
            return cls(variables, df, perf)
        else:
            return cls(variables)

    def __init__(
        self,
        variables: Dict[str, Variable],
        data: Optional[pd.DataFrame] = None,
        perf: Optional[Performance] = None,
    ):
        self._model = Model(None, variables, name="init")
        self.variables = variables
        self._models = []  # list of all fitted models

        self.data: Optional[pd.DataFrame] = data
        self.perf: Optional[Performance] = perf

        self._eval_sets: EvalSets = None

    @property
    def eval_sets(self):
        return self._eval_sets

    @eval_sets.setter
    def eval_sets(self, eval_sets: EvalSets):
        # TODO: do some checks here of data and perf
        self._eval_sets = eval_sets

    def _check_data(self, df, perf):
        args_populated = df is not None and perf is not None
        self_populated = self.data is not None and self.perf is not None

        if args_populated:
            return df, perf
        elif self_populated:
            return self.data, self.perf
        else:
            raise Exception(
                "df and perf must be provided together or data, perf attributes must be set in Scorecard"
            )

    def summary(
        self, df: Optional[pd.DataFrame] = None, perf: Optional[Performance] = None
    ):
        # create summary dataframe for each variable
        df, perf = self._check_data(df, perf)

        res = []
        for v in self.variables.values():
            res.append(v.summary(df[v.name], perf))
        return pd.DataFrame.from_records(res)

    def to_sparse(
        self,
        df: Optional[pd.DataFrame] = None,
        step: List[Optional[int]] = [1],
    ):
        if df is None and self.data is not None:
            df = self.data

        res = []
        for k, v in self.variables.items():
            if v.step in step:
                res.append(v.to_sparse(df[k]))
        M = hstack(res)
        return hstack([M, np.ones(M.shape[0]).reshape(-1, 1)])

    def __getitem__(self, key):
        return self.variables[key]

    @property
    def model(self) -> Model:
        return self._model

    @model.setter
    def model(self, mod: Model):
        self._model = mod

    @property
    def models(self):
        return [m.name for m in self._models]

    def get_constraints(self, step=[1]):
        """get constraints for SLQSP optimizer"""
        # {"type": type_, "indices": indices, "len": len(labels)}
        i, res = 0, []
        for v in self.variables.values():
            if v.step in step:
                n, constraints = v.get_constraints()
                for constr in constraints:
                    # create constraint dict for SLQSP optimizer
                    base, target = constr["indices"]
                    type_ = constr["type"]

                    if type_ == "neu":
                        fun = eval(f"lambda x: x[{base + i}]")
                        res.append({"type": "eq", "fun": fun})
                    else:
                        fun = eval(f"lambda x: x[{base + i}] - x[{target + i}]")
                        if type_ == "=":
                            res.append({"type": "eq", "fun": fun})
                        else:
                            res.append({"type": "ineq", "fun": fun})
                i += n

        return res

    def save_model(self, mod: Model):
        self._models.append(mod)
        self.model = mod

    def load_model(self, model_id: Union[str, int]):
        """load fitted model and variables as they existed when the selected model was fit"""
        if isinstance(model_id, str):
            model = None
            for m in self._models:
                if m.name == model_id:
                    model = m
            if model is not None:
                self.model = model
                self.variables = copy.deepcopy(model.variables)
            else:
                raise Exception(f"no model found with name: {model_id}")
        elif isinstance(model_id, int):
            if (model_id < len(self._models) - 1) and (model_id > 0):
                model = self._models[model_id]
                self.model = model
                self.variables = copy.deepcopy(model.variables)
            else:
                raise Exception(f"invalid model index: {model_id}")
        else:
            raise Exception("model_id must be model name or a model index")

    def predict(self, df: Optional[pd.DataFrame] = None):
        if df is None and self.data is not None:
            df = self.data
        if self.model is None:
            raise Exception("no models have been fit yet")
        M = self.to_sparse(df)
        return M @ self.model.coefs

    def fit(
        self,
        df: Optional[pd.DataFrame] = None,
        perf: Optional[Performance] = None,
        offset: Optional[np.ndarray] = None,
        alpha: float = 0.001,
    ):

        df, perf = self._check_data(df, perf)

        M = self.to_sparse(df)

        if offset is None:
            offset = np.zeros(M.shape[0])

        constraints = self.get_constraints()

        coefs = np.zeros(M.shape[1])

        obj = minimize(
            logistic_loss,
            coefs,
            (M, perf.y.values, perf.w.values, offset, alpha),
            jac=logistic_gradient,
            method="SLSQP",
            constraints=constraints,
        )

        self.save_model(Model(obj, self.variables, f"model_{len(self._models):02d}"))

    def display_variable(
        self,
        var: str,
        df: Optional[pd.DataFrame] = None,
        perf: Optional[Performance] = None,
        eval_sets: bool = False,
    ):
        df, perf = self._check_data(df, perf)

        # TODO: merge fit data
        res = [self[var].display(df[var], perf)]

        if eval_sets and self.eval_sets is not None:
            for df, perf in self.eval_sets:
                res.append(self[var].display(df[var], perf))

        return pd.concat(res, axis=0)


def sigmoid(x):
    return ne.evaluate("1 / (1 + exp(-x))")


def logistic_loss(coefs, X, y, w, offset, alpha=0.001) -> float:
    """w is always provided as an array of 1s at the very least"""

    h = sigmoid((X @ coefs) + offset)
    m = ne.evaluate("sum(w)")

    weighted_cost = ne.evaluate("(y * log(h) + (1 - y) * log(1 - h)) * w")
    cost = -np.sum(weighted_cost) / m

    # regularization
    reg = (alpha * np.sum(coefs[1:] ** 2)) / 2
    return cost + reg


def logistic_gradient(coefs, X, y, w, offset, alpha=0.001) -> float:
    h = sigmoid((X @ coefs) + offset)

    grads = X.T @ (w * (h - y))  # multiple error by weight

    # ignore intercept during update
    grads[:-1] = grads[:-1] + alpha * grads[:-1]

    return grads / w.sum()

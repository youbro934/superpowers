"""Count regression (Poisson GLM, log link, ridge) with negative-binomial dispersion,
plus the discrete distributions the market layer needs. Requires numpy."""
from __future__ import annotations

import math

import numpy as np

MAX_COUNT = 60


class CountModel:
    def __init__(self, ridge: float = 2.0, max_iter: int = 30):
        self.ridge = ridge
        self.max_iter = max_iter
        self.beta: np.ndarray | None = None
        self.alpha = 0.0          # NB dispersion: var = mu + alpha * mu^2
        self.n = 0

    def fit(self, X: np.ndarray, y: np.ndarray, offset: np.ndarray | None = None) -> "CountModel":
        n, k = X.shape
        Xi = np.hstack([np.ones((n, 1)), X])
        off = offset if offset is not None else np.zeros(n)
        beta = np.zeros(k + 1)
        beta[0] = math.log(max(y.mean(), 1e-3)) - off.mean()
        pen = np.eye(k + 1) * self.ridge
        pen[0, 0] = 0.0
        for _ in range(self.max_iter):
            eta = np.clip(Xi @ beta + off, -10, 6)
            mu = np.exp(eta)
            z = eta - off + (y - mu) / mu
            W = mu
            A = Xi.T @ (Xi * W[:, None]) + pen
            b = Xi.T @ (W * z)
            new = np.linalg.solve(A, b)
            if np.max(np.abs(new - beta)) < 1e-7:
                beta = new
                break
            beta = new
        self.beta = beta
        mu = np.exp(np.clip(Xi @ beta + off, -10, 6))
        # method-of-moments NB dispersion; 0 means Poisson is adequate
        self.alpha = float(max(0.0, np.sum((y - mu) ** 2 - mu) / np.sum(mu ** 2)))
        self.n = n
        return self

    def predict(self, X: np.ndarray, offset: np.ndarray | None = None) -> np.ndarray:
        Xi = np.hstack([np.ones((X.shape[0], 1)), X])
        off = offset if offset is not None else np.zeros(X.shape[0])
        return np.exp(np.clip(Xi @ self.beta + off, -10, 6))


def count_pmf(mu: float, alpha: float, max_k: int = MAX_COUNT) -> np.ndarray:
    k = np.arange(max_k + 1)
    if alpha < 1e-6:
        logp = -mu + k * math.log(max(mu, 1e-12)) - np.array([math.lgamma(i + 1) for i in k])
    else:
        r = 1.0 / alpha
        p = r / (r + mu)
        logp = (np.array([math.lgamma(i + r) - math.lgamma(r) - math.lgamma(i + 1) for i in k])
                + r * math.log(p) + k * math.log(max(1 - p, 1e-12)))
    pmf = np.exp(logp)
    return pmf / pmf.sum()


def convolve(a: np.ndarray, b: np.ndarray, max_k: int = MAX_COUNT) -> np.ndarray:
    c = np.convolve(a, b)[: max_k + 1]
    return c / c.sum()


def p_over(pmf: np.ndarray, line: float) -> float:
    return float(pmf[int(math.floor(line)) + 1:].sum())

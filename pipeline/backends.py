"""Fixed learners. CUDA failure is fatal, never a CPU-labelled GPU run."""
import json
import warnings
from sklearn.ensemble import HistGradientBoostingClassifier

BACKENDS = ("auto", "cpu", "xgb-cpu", "xgb-cuda")
# Features are built on the CPU, so each prediction needs one host-to-GPU copy. XGBoost makes that
# copy itself and still predicts on the GPU; pre-placing data on the device would save only the copy
# (about 50 MB per million rows, milliseconds over PCIe), so its advisory warning is noise here.
warnings.filterwarnings("ignore", message=r".*Falling back to prediction using DMatrix due to mismatched devices.*")


def cuda_available():
    """A real one-round CUDA fit, not a driver query: proves the device can train."""
    try:
        import numpy as np
        from xgboost import XGBClassifier
        model = XGBClassifier(n_estimators=1, device="cuda:0", tree_method="hist")
        model.fit(np.array([[0.], [1.], [0.], [1.]]), np.array([0, 1, 0, 1]))
        verify_device(model, "xgb-cuda")
        return True
    except Exception:
        return False


def resolve(backend):
    """'auto' = the GPU when CUDA trains, else the same XGBoost model on CPU."""
    if backend != "auto":
        return backend
    return "xgb-cuda" if cuda_available() else "xgb-cpu"


def outcome_model(backend="cpu", seed=0):
    if backend == "cpu":
        return HistGradientBoostingClassifier(max_iter=300, learning_rate=.05,
            max_leaf_nodes=31, min_samples_leaf=200, l2_regularization=1., random_state=seed)
    if backend not in ("xgb-cpu", "xgb-cuda"):
        raise ValueError(f"Unknown backend: {backend}")
    from xgboost import XGBClassifier
    return XGBClassifier(n_estimators=300, learning_rate=.05, max_depth=5,
        min_child_weight=50, reg_lambda=1., tree_method="hist", max_bin=256,
        device="cuda:0" if backend == "xgb-cuda" else "cpu", n_jobs=8,
        random_state=seed, eval_metric="logloss")


def verify_device(model, backend):
    if backend == "xgb-cuda":
        device = json.loads(model.get_booster().save_config())["learner"]["generic_param"]["device"]
        if not device.startswith("cuda"):
            raise RuntimeError("XGBoost did not train on CUDA; refusing a misleading GPU result")

from typing import Tuple

import tensorflow as tf
from tensorflow.keras import backend as K
from tensorflow.keras.metrics import Metric
from tensorflow.python.ops import weights_broadcast_ops
from tensorflow_addons.utils.types import AcceptableDTypes
from typeguard import typechecked

VALID_MULTIOUTPUT = frozenset(
    {'raw_values', 'uniform_average', 'variance_weighted'}
)


def _reduce_average(
    input_tensor: tf.Tensor, axis=None, keepdims=False, weights=None
) -> tf.Tensor:
    """Computes the (weighted) mean of elements across dimensions of a tensor."""
    if weights is None:
        return tf.reduce_mean(input_tensor, axis=axis, keepdims=keepdims)

    weighted_sum = tf.reduce_sum(
        weights * input_tensor, axis=axis, keepdims=keepdims
    )
    sum_of_weights = tf.reduce_sum(weights, axis=axis, keepdims=keepdims)
    return weighted_sum / sum_of_weights


class RSquare(Metric):
    """Compute R^2 score.
     This is also called the [coefficient of determination
     ](https://en.wikipedia.org/wiki/Coefficient_of_determination).
     It tells how close are data to the fitted regression line.
     - Highest score can be 1.0 and it indicates that the predictors
       perfectly accounts for variation in the target.
     - Score 0.0 indicates that the predictors do not
       account for variation in the target.
     - It can also be negative if the model is worse.
     The sample weighting for this metric implementation mimics the
     behaviour of the [scikit-learn implementation
     ](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.r2_score.html)
     of the same metric.
     Usage:
     ```python
     actuals = tf.constant([1, 4, 3], dtype=tf.float32)
     preds = tf.constant([2, 4, 4], dtype=tf.float32)
     result = tf.keras.metrics.RSquare()
     result.update_state(actuals, preds)
     print('R^2 score is: ', r1.result().numpy()) # 0.57142866
    ```
    """

    @typechecked
    def __init__(
        self,
        name: str = 'r_square',
        dtype: AcceptableDTypes = None,
        y_shape: Tuple[int, ...] = (),
        multioutput: str = 'uniform_average',
        **kwargs,
    ):
        super().__init__(name=name, dtype=dtype, **kwargs)
        self.y_shape = y_shape

        if multioutput not in VALID_MULTIOUTPUT:
            raise ValueError(
                'The multioutput argument must be one of {}, but was: {}'.format(
                    VALID_MULTIOUTPUT, multioutput
                )
            )
        self.multioutput = multioutput
        self.squared_sum = self.add_weight(
            name='squared_sum', shape=(), initializer='zeros', dtype=dtype
        )
        self.sum = self.add_weight(
            name='sum', shape=(), initializer='zeros', dtype=dtype
        )
        self.res = self.add_weight(
            name='residual', shape=(), initializer='zeros', dtype=dtype
        )
        self.count = self.add_weight(
            name='count', shape=(), initializer='zeros', dtype=dtype
        )

    def update_state(self, y_true, y_pred, sample_weight=None) -> None:
        y_true = tf.cast(y_true, dtype=self._dtype)
        y_pred = tf.cast(y_pred, dtype=self._dtype)
        y_pred = tf.squeeze(y_pred)
        if sample_weight is None:
            sample_weight = 1
        sample_weight = tf.cast(sample_weight, dtype=self._dtype)
        sample_weight = weights_broadcast_ops.broadcast_weights(
            weights=sample_weight, values=y_true
        )

        weighted_y_true = y_true * sample_weight
        self.sum.assign_add(tf.reduce_sum(weighted_y_true, axis=0))
        self.squared_sum.assign_add(
            tf.reduce_sum(y_true * weighted_y_true, axis=0)
        )
        self.res.assign_add(
            tf.reduce_sum(tf.square(y_true - y_pred) * sample_weight, axis=0)
        )
        self.count.assign_add(tf.reduce_sum(sample_weight, axis=0))

    def result(self) -> tf.Tensor:
        mean = self.sum / self.count
        total = self.squared_sum - self.sum * mean
        raw_scores = 1 - (self.res / total)

        if self.multioutput == 'raw_values':
            return raw_scores
        if self.multioutput == 'uniform_average':
            return tf.reduce_mean(raw_scores)
        if self.multioutput == 'variance_weighted':
            return _reduce_average(raw_scores, weights=total)
        raise RuntimeError(
            'The multioutput attribute must be one of {}, but was: {}'.format(
                VALID_MULTIOUTPUT, self.multioutput
            )
        )

    def reset_states(self) -> None:
        # The state of the metric will be reset at the start of each epoch.
        K.batch_set_value([(v, tf.zeros_like(v)) for v in self.variables])

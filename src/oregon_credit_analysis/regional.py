"""Keep native weights while binding household vectors to authenticated IDs."""

import pandas as pd
from microdf import MicroSeries


def index_by_household_id(values, household_ids):
    """Relabel only; the native values, precision and weights remain unchanged."""
    if not isinstance(values, MicroSeries):
        raise ValueError("Household values must retain their native MicroSeries")
    if not values.index.equals(values.weights.index):
        raise ValueError("Native values and weights have different indexes")
    ids = pd.Index(household_ids, name="household_id")
    if len(ids) != len(values) or ids.has_duplicates or ids.hasnans:
        raise ValueError("Household IDs must be complete, unique and aligned")
    # MicroSeries.set_weights position-aligns these existing native weights at
    # the new ID index. No weight is calculated, replaced, or aggregated here.
    return MicroSeries(
        pd.Series(values, copy=False).set_axis(ids), weights=values.weights
    )


def native_vectors_equal_by_id(left, right):
    """Require exactly the same records, dtypes, native values and weights."""
    if not isinstance(left, MicroSeries) or not isinstance(right, MicroSeries):
        raise ValueError("Comparisons require native MicroSeries")
    if left.index.has_duplicates or right.index.has_duplicates:
        raise ValueError("Duplicate household IDs")
    if set(left.index) != set(right.index):
        return False
    return left[right.index].equals(right)

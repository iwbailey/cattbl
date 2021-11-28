"""Module for working with a period event loss table"""
import warnings
import pandas as pd
import numpy as np


COL_YEAR = 'Year'
COL_EVENT = 'EventID'
COL_DAY = 'DayOfYear'
COL_LOSS = 'Loss'
INDEX_NAMES = [COL_YEAR, COL_DAY, COL_EVENT]


@pd.api.extensions.register_series_accessor("yelt")
class YearEventLossTable:
    """Accessor for a Year event loss table as a series.

    The pandas series should have a MultiIndex with unique combinations of
    Year, EventID, DayOfYear (all of int type) and its values contain the
    losses. There should be an attribute called 'n_yrs'
    """
    def __init__(self, pandas_obj):
        """Validate the series for use with accessor"""
        self._validate(pandas_obj)
        self._obj = pandas_obj

    @staticmethod
    def _validate(obj):
        """Check it is a valid YELT series"""

        # Check the index names
        if len(obj.index.names) != 3:
            raise AttributeError("Expecting 3 index levels")

        if not all([c in obj.index.names for c in INDEX_NAMES]):
            raise AttributeError(f"Expecting index names {INDEX_NAMES}")

        # Check indices are all integer types
        if not all([pd.api.types.is_integer_dtype(c)
                    for c in obj.index.levels]):
            raise TypeError("Indices must all be integer types. Currently: " +
                            f"{[c.dtype for c in obj.index.levels]}")

        # Check the series is numeric
        if not pd.api.types.is_numeric_dtype(obj):
            raise TypeError(f"Series should be numeric. It is {obj.dtype}")

        # Check n_yrs stored in attributes
        if 'n_yrs' not in obj.attrs.keys():
            raise AttributeError("Must have 'n_yrs' in the series attrs")

        # Check unique
        if not obj.index.is_unique:
            raise AttributeError(f"Combinations of {INDEX_NAMES} not unique")

        # Check the years are within range 1, n_yrs
        years = obj.index.get_level_values(COL_YEAR)
        if years.min() < 1 or years.max() > obj.attrs['n_yrs']:
            raise AttributeError("Years in index are out of range 1,n_yrs")

    @property
    def is_valid(self):
        """Check we can pass the initialisation checks"""
        return True

    @property
    def n_yrs(self):
        """Return the number of years for the ylt"""
        return self._obj.attrs['n_yrs']

    @property
    def aal(self):
        """Return the average annual loss"""
        return self._obj.sum() / self.n_yrs

    @property
    def freq0(self):
        """Frequency of a loss greater than zero"""
        return (self._obj > 0).sum() / self.n_yrs

    def to_ylt(self, is_occurrence=False):
        """Convert to a YLT

        If is_occurrence return the max loss in a year. Otherwise return the
        summed loss in a year.
        """

        yrgroup = self._obj.groupby('Year')

        if is_occurrence:
            return yrgroup.max()
        else:
            return yrgroup.sum()

    def exfreq(self, **kwargs):
        """For each loss calculate the frequency >= loss"""
        return (self._obj.rank(ascending=False, method='min', **kwargs)
                .divide(self.n_yrs)
                .rename('ExFreq')
                )

    def to_ef_curve(self, keep_index=False, **kwargs):
        """Return an Exceedance frequency curve"""

        # Create the dataframe by combining loss with exfreq
        ef_curve = pd.concat([self._obj.copy(), self._obj.yelt.exfreq(**kwargs)],
                             axis=1)

        # Sort from largest to smallest loss
        ef_curve = (ef_curve
                    .reset_index()
                    .sort_values(
                        by=['Loss', 'ExFreq', 'Year', 'DayOfYear'],
                        ascending=(False, True, False, False)))

        if not keep_index:
            ef_curve = ef_curve.drop(INDEX_NAMES, axis=1).drop_duplicates()

        # Reset the index
        ef_curve = ef_curve.reset_index(drop=True)
        ef_curve.index.name = 'Order'

        return ef_curve

    def loss_at_rp(self, return_periods, **kwargs):
        """Interpolate the year loss table for losses at specific return periods

        :param return_periods: [numpy.array] should be ordered from largest to
        smallest. A list will also work.

        :returns: [numpy.array] losses at the corresponding return periods

        The interpolation is done on exceedance probability.
        Values below the smallest exceedance probability get the max loss
        Values above the largest exceedance probability get zero
        Invalid exceedance return periods get NaN
        """

        # Get the full EP curve
        ef_curve = self.to_ef_curve(**kwargs)

        # Get the max loss for the high return periods
        max_loss = ef_curve['Loss'].iloc[0]

        # Remove invalid return periods
        return_periods = np.array(return_periods).astype(float)
        return_periods[return_periods <= 0.0] = np.nan

        losses = np.interp(1 / return_periods,
                           ef_curve['ExFreq'],
                           ef_curve['Loss'],
                           left=max_loss, right=0.0)

        return losses

    def cprob(self, **kwargs):
        """Calculate the empiric conditional cumulative probability of loss size

        CProb = Prob(X<=x|Loss has occurred) where X is the event loss, given a
        loss has occurred.
        """
        return (self._obj.rank(ascending=True, method='max', **kwargs)
                .divide(len(self._obj))
                .rename('CProb')
                )

    def to_severity_curve(self, keep_index=False, **kwargs):
        """Return a severity curve. Cumulative prob of loss size."""

        # Create the dataframe by combining loss with cumulative probability
        sev_curve = pd.concat([self._obj.copy(),
                               self._obj.yelt.cprob(**kwargs)],
                              axis=1)

        # Sort from largest to smallest loss
        sev_curve = (sev_curve
                     .reset_index()
                     .sort_values(
                         by=['Loss', 'CProb', 'Year', 'DayOfYear'],
                         ascending=(True, True, True, True)))

        if not keep_index:
            sev_curve = sev_curve.drop(INDEX_NAMES, axis=1).drop_duplicates()

        # Reset the index
        sev_curve = sev_curve.reset_index(drop=True)
        sev_curve.index.name = 'Order'

        return sev_curve

    def apply_layer(self, limit=None, xs=0.0, n_loss=None, is_franchise=False):
        """Calculate the loss to a layer for each event"""

        assert xs >= 0, "Lower loss must be >= 0"

        # Apply layer attachment and limit
        layer_losses = (self._obj
                        .subtract(xs).clip(lower=0.0)
                        .clip(upper=limit)
                        )

        # Keep only non-zero losses to make the next steps quicker
        layer_losses = layer_losses.loc[layer_losses > 0]

        if is_franchise:
            layer_losses += xs

        # Apply occurrence limit
        if n_loss is not None:
            layer_losses = (layer_losses
                            .sort_index(level=[COL_YEAR, COL_DAY, COL_EVENT])
                            .groupby(COL_YEAR).head(n_loss)
                            )

        return layer_losses

    def layer_aal(self, **kwargs):
        """Calculate the AAL within a layer

        :param kwargs: passed to .apply_layer
        """

        layer_losses = self.apply_layer(**kwargs)

        return layer_losses.sum() / self.n_yrs

    def to_ep_summary(self, return_periods, is_occurrence=False, **kwargs):
        """Get loss at summary return periods and return a pandas Series

        :returns: [pands.Series] with index 'ReturnPeriod' and Losses at each
        of those return periods
        """

        ylt = self.to_ylt(is_occurrence)

        return pd.Series(ylt.ylt.loss_at_rp(return_periods, **kwargs),
                         index=pd.Index(return_periods, name='ReturnPeriod'),
                         name='Loss')

    def to_ep_summaries(self, return_periods, is_eef=True, **kwargs):
        """Return a dataframe with multiple EP curves side by side"""

        aep = self.to_ep_summary(return_periods, is_occurrence=False, **kwargs)
        oep = self.to_ep_summary(return_periods, is_occurrence=True, **kwargs)

        aep = aep.rename('LossPerYear')
        oep = oep.renaem('MaxEventLossPerYear')

        if is_eef:
            eef = self.loss_at_rp(return_periods, **kwargs)
            eef = eef.rename('EventLoss')
            combined = pd.concat([aep, oep, eef], axis=1)
        else:
            combined = pd.concat([aep, oep], axis=1)

        return combined


def from_cols(year, eventid, dayofyear, loss, n_yrs):
    """Create a pandas Series YELT (Year Event Loss Table) from its columns

    :param year: a list or array af integer year numbers starting at 1

    :param eventid: a list or array of integer event IDs

    :param dayofyear: a list or array of integer days within a year

    :param loss: a list or array of numeric losses

    :param n_yrs: [int] the total number of years in the table

    All column inputs should be the same length. The year, eventid, dayofyear
    combinations should all be unique.

    :returns: (pandas.Series) compatible with the accessor yelt
    """

    new_yelt = pd.DataFrame({COL_YEAR: year,
                             COL_EVENT: eventid,
                             COL_DAY: dayofyear,
                             COL_LOSS: loss})
    try:
        new_yelt.set_index([COL_YEAR, COL_EVENT, COL_DAY],
                           verify_integrity=True, inplace=True)
    except ValueError:
        raise ValueError("You cannot have duplicate combinations of " +
                         f"{INDEX_NAMES}")

    new_yelt.attrs['n_yrs'] = n_yrs

    return new_yelt[COL_LOSS]


def from_df(df, n_yrs=None):
    """Create a pandas Series YELT (Year Event Loss Table) from a DataFrame

    :param df: [pandas.DataFrame] see from_cols for details of column names

    :param n_yrs: [int] the total number of years in the table

    :returns: (pandas.Series) compatible with the accessor yelt
    """

    if n_yrs is None:
        n_yrs = df.attrs['n_yrs']

    # Reset index in case
    df = df.reset_index(drop=False)

    # Convert existing columns to lower case so we handle different input
    # options for column case
    df.columns = [c.lower() for c in df.columns]

    # Convert the types if necessary
    for col in [c for c in INDEX_NAMES]:
        col2 = col.lower()
        if not pd.api.types.is_integer_dtype(df[col2]):
            warnings.warn(f"{col} is {df[col2].dtype} " +
                          "and will be forced to int type")
            df[col2] = df[col2].astype(np.int64)

    # Set up the YELT
    yelt = from_cols(year=df[COL_YEAR.lower()],
                     eventid=df[COL_EVENT.lower()],
                     dayofyear=df[COL_DAY.lower()],
                     loss=df[COL_LOSS.lower()],
                     n_yrs=n_yrs)
    return yelt


def from_csv(ifile, n_yrs):
    """Create a pandas Series YELT (Year Event Loss Table) from a csv file

    :param ifile: [str] file passed to pandas.read_csv see from_cols for details
     of expected column names

    :param n_yrs: [int] the total number of years in the table

    :returns: (pandas.Series) compatible with the accessor yelt
    """

    df = pd.read_csv(ifile, usecols=INDEX_NAMES + [COL_LOSS])

    return from_df(df, n_yrs)

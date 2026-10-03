"""Venue-native proportional fees, shared by sizing, labels and simulated fills.

Binance TH buys pay the fee in received base units; sells in received quote.
Published rates are assumptions, not an authenticated account fee schedule.
"""
from .domain import ONE


def entry_cash_factor(cfg):
    return ONE if cfg.venue == "binance_th" else ONE + cfg.costs.taker_fee


def entry_inventory_factor(cfg):
    return ONE - cfg.costs.taker_fee if cfg.venue == "binance_th" else ONE

# Paper-Trading Research

This context describes deterministic experiments that compare trading strategies with simulated capital and live or replayed market observations.

## Language

**Experiment Run**:
A time-bounded, immutable comparison in which every participating portfolio receives the same relevant market observations.
_Avoid_: Test, session, current experiment

**Strategy**:
A frozen deterministic rule set that turns market and portfolio state into order intents.
_Avoid_: Algorithm instance, signal bot

**Portfolio**:
Independent virtual capital assigned to one strategy within an experiment run. A portfolio may own several position legs.
_Avoid_: Account, wallet

**Trade**:
A strategy-directed exposure lifecycle that begins with entry and ends when all of its legs are closed.
_Avoid_: Position, order

**Leg**:
One instrument exposure within a trade, such as long BTC spot or short BTC perpetual.
_Avoid_: Trade, position

**Order Intent**:
A strategy's deterministic request to open or close one or more trade legs; it is not an exchange order.
_Avoid_: Signal, real order

**Pending Order**:
An accepted post-only order intent waiting for a conservative simulated fill before its expiry.
_Avoid_: Open trade, live order

**Fill**:
A simulated execution of an order intent under the experiment's frozen spread, slippage, and fee rules.
_Avoid_: Order, signal

**Market Observation**:
A timestamped normalized view of prices, trades, candles, or funding supplied to every eligible strategy.
_Avoid_: Tick, event

**Funding Payment**:
A simulated hourly transfer attributed to an open perpetual leg using the observed funding rate.
_Avoid_: Fee, interest

**Benchmark**:
A non-candidate portfolio used to interpret strategy performance, such as cash or buy-and-hold BTC.
_Avoid_: Strategy winner, control account

# Common Card Fraud Patterns

*Fictional guide for the PayShield demo project.*

## Card testing

In card testing, criminals who have bought or stolen a list of card numbers check which ones still work. They make many small payments in quick succession, often a few dollars each at online merchants, because small amounts are less likely to be noticed. A burst of tiny payments within a minute is the classic sign, which is what the too_fast rule looks for. Once a card passes the test, a much larger fraudulent purchase usually follows.

## Stolen card used far away

When card details are copied, for example by a skimming device or a data breach, they are often used in a different city or country from the real cardholder. If the same card appears at two places that are too far apart to travel between in the time available, at least one of the payments is almost certainly not made by the cardholder. This is the pattern behind the impossible_travel rule.

## Sudden big purchases

Fraudsters want to get as much value as possible before the card is blocked, so stolen cards are often used for one or a few purchases that are much larger than the cardholder usually spends. Electronics, gift cards and online shopping are common targets. A sharp jump compared with the card's normal spending is what the unusual_amount rule detects.

## Late-night online spending

In historical fraud data, a large share of fraud happens late at night and in online categories such as online shopping and miscellaneous online purchases. Genuine cardholders are mostly asleep, so they notice the fraud later. The machine-learning model learns patterns like this from the hour of day and merchant category, even when none of the simple rules fire.

## False positives

No fraud system is perfect. Good customers sometimes travel, make one large purchase, or pay several times quickly at a busy event. A declined genuine payment is called a false positive. It annoys customers, so the decline threshold is a trade-off between catching more fraud and declining fewer good payments. Explaining the reason for a decline clearly helps genuine customers resolve it quickly.

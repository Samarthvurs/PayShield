# PayShield Decline Codes

*Fictional guide for the PayShield demo project.*

## Code 00: Approved

The payment was approved and the amount was deducted from the card balance. No action is needed.

## Code 05: Card blocked

The card has been blocked by the issuer, for example after it was reported lost or stolen, so no payments can be made with it. What the cardholder should do: contact the bank that issued the card. Retrying the payment will not help until the card is unblocked or replaced.

## Code 14: Invalid card

The card number is not valid or is not known to PayShield. This happens when the number fails the Luhn checksum (usually a typing mistake), when the number does not belong to any card, or when the expiry date does not match the card. What the cardholder should do: check the card number and expiry date and try again.

## Code 51: Insufficient funds

The card does not have enough available balance for this amount. What the cardholder should do: use a smaller amount, add funds, or pay with a different card.

## Code 54: Expired card

The card's expiry date has passed. A card stays valid until the last day of its expiry month. What the cardholder should do: use the replacement card sent by the bank, or ask the bank for a new card.

## Code 59: Suspected fraud

The payment was declined because its fraud risk score reached the decline threshold. The score combines three rules (payment velocity, unusual amount and impossible travel) with a machine-learning model. What the cardholder should do: if they made the payment themselves, contact the bank to confirm it is genuine and then retry. If they did not make it, they should report the card as compromised so it can be blocked and replaced.

# PayShield Fraud Rules Guide

*Fictional guide for the PayShield demo project.*

## How the risk score works

Every payment gets a risk score from 0 to 100. It combines two parts with equal weight: the rule score (from three simple rules) and the machine-learning score. A payment whose combined risk score reaches the decline threshold is declined with response code 59, "Suspected fraud". The threshold is chosen when the model is trained, by finding the value that best balances catching fraud against wrongly declining good payments. If no model is trained yet, the rule score alone is used with a threshold of 60.

## Rule: too_fast (payment velocity)

The too_fast rule fires when a card makes more than five payments within 60 seconds. It adds 35 points to the rule score. Real cardholders rarely pay six times in one minute. This pattern usually means card testing: a fraudster who has stolen card details runs many small payments quickly to find out whether the card works before making a large purchase. What to do: the cardholder should check recent payments they don't recognise and contact their bank to replace the card.

## Rule: unusual_amount (spending spike)

The unusual_amount rule fires when the amount is more than three standard deviations above the card's usual spending. It adds 30 points. The rule only applies once the card has at least five earlier payments, because before that there is no reliable picture of normal spending. A standard deviation measures how much a card's payments usually vary; three standard deviations above the average is very rare for genuine purchases. This often indicates a stolen card being used for one large purchase. A genuine large purchase, such as a holiday or a new laptop, can also trigger it, so the cardholder can simply confirm it and retry.

## Rule: impossible_travel (location jump)

The impossible_travel rule fires when the distance between this payment and the card's previous payment would require travelling faster than 900 km/h, roughly the speed of a passenger jet. It adds 35 points. A card physically used in two distant cities within minutes usually means the card details were copied and are being used by someone else in a different place. Online payments with an inaccurate merchant location can occasionally cause false alarms.

## The machine-learning score

The machine-learning score is the fraud probability predicted by a gradient boosting model, multiplied by 100. The model was trained on about 1.3 million historical transactions from a public, simulated card dataset. It looks at the amount, how unusual the amount is for the card, how many payments the card made in the last hour and day, time since the previous payment, distance and speed from the previous payment, hour of day and merchant category. It can catch combinations the simple rules miss, for example a moderately large online purchase late at night from a card that normally shops in person during the day. A high ML score alone is a strong signal but not proof of fraud.

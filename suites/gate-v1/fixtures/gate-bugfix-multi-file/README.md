# shipping

Small shipping-fee library.

Contract (every public function follows it):

- `rate_for(zone)` returns the per-kg rate of a zone. Zone names are case-insensitive (`"a"` is zone `"A"`).
- `billable_weight(kg)` rounds the weight **up** to the next 0.5 kg step (1.2 -> 1.5, 2.0 -> 2.0).
- `quote(zone, kg)` is `rate_for(zone) * billable_weight(kg)` rounded to 2 decimals.
- Unknown zones, a zone that is not a string, and a weight that is not a positive number all raise
  `shipping.errors.ShippingError`. No other exception type may leak out of the public functions.

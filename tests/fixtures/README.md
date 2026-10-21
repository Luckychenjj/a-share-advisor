# Volume regression snapshot

`volume_sample_20260930.json` contains public raw Tencent, Eastmoney and Sina
responses retrieved during the October 2, 2026 holiday. Each entry records its URL.
The quotes and latest daily bars are for September 30, not the retrieval date.

- Tencent quote field 6 and both daily K-line providers: 243,948 lots.
- Sina individual-stock field 8: 24,394,752 shares (243,947.52 lots).
- Five sessions preceding September 30: 346,346; 227,235; 250,150; 387,037; 254,415 lots.
- Average: 293,036.6 lots. Independently calculated closing ratio: 0.8324830414.
- Tencent field 49: 0.83; field 30: 20260930161430 (China time).

The amount/volume implied price must also remain between the daily low and high.
That independent check catches an incorrect 100-fold conversion.

def add_ema(df):
    df = df.copy()

    # Normalize column name to 'close'
    if "close" not in df.columns:
        if "Close" in df.columns:
            df.rename(columns={"Close": "close"}, inplace=True)
        else:
            raise KeyError(f"'close' column not found. Columns: {list(df.columns)}")

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    return df
<<<<<<< HEAD
=======


>>>>>>> d3468d04cd2f9d38901632a83bf2863506e40df9

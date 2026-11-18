"""Read-only capability checks of the public competition RPC proxy."""

from collect import rpc, write


def main():
    results = []
    for method, params in (
        ("web3_clientVersion", []),
        ("txpool_status", []),
        ("eth_getBlockByNumber", ["latest", False]),
        ("eth_getBlockByNumber", ["pending", False]),
    ):
        row = {"method": method, "params": params}
        try:
            data = rpc(method, params)
            if isinstance(data, dict) and "transactions" in data:
                row["result"] = {
                    k: data.get(k) for k in ("number", "hash", "timestamp")
                }
                row["transaction_count"] = len(data["transactions"])
            else:
                row["result"] = data
        except (ValueError, RuntimeError) as exc:
            row["error"] = str(exc)
        results.append(row)
        print(row, flush=True)
    write("rpc_capabilities.json", results)


if __name__ == "__main__":
    main()

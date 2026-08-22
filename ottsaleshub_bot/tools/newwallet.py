"""Generate a deposit wallet. RUN THIS ON YOUR OWN COMPUTER, NEVER ON A SERVER.

    pip install bip_utils
    python tools/newwallet.py

Prints a seed phrase and the matching account xpub.

  * The SEED goes on paper. It is the only thing that can move your money, and
    nobody can restore it for you if it is lost.
  * The XPUB goes in Render as EVM_XPUB. It can derive addresses and watch them;
    it cannot spend. If your server is breached, this is all the attacker gets.

Verify before taking any payment: import the seed into MetaMask and check its
first addresses against the ones printed here. If they differ, do not deploy —
buyers would be sending to addresses your seed cannot reach, and that is not
recoverable.
"""
from bip_utils import (Bip39MnemonicGenerator, Bip39SeedGenerator, Bip39WordsNum,
                       Bip44, Bip44Changes, Bip44Coins)


def main(words: int = 12, preview: int = 5) -> None:
    mnemonic = Bip39MnemonicGenerator().FromWordsNumber(
        Bip39WordsNum.WORDS_NUM_24 if words == 24 else Bip39WordsNum.WORDS_NUM_12)
    seed = Bip39SeedGenerator(mnemonic).Generate()
    acct = (Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
            .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT))

    print("\n" + "=" * 70)
    print("SEED PHRASE — write this on paper, then close this window")
    print("=" * 70)
    print(f"\n  {mnemonic}\n")
    print("=" * 70)
    print("EVM_XPUB — paste this into Render (safe to store, cannot spend)")
    print("=" * 70)
    print(f"\n  {acct.PublicKey().ToExtended()}\n")
    print("=" * 70)
    print(f"First {preview} addresses — these must match your wallet exactly")
    print("=" * 70)
    for i in range(preview):
        print(f"  m/44'/60'/0'/0/{i}  {acct.AddressIndex(i).PublicKey().ToAddress()}")
    print("\nImport the seed into MetaMask. Its first accounts must be the")
    print("addresses above, in this order. If they are not, stop.\n")


if __name__ == "__main__":
    main()

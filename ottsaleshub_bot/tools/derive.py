"""Derive ONE deposit account from your seed. RUN ON YOUR OWN COMPUTER ONLY.

    pip install bip_utils
    python tools/derive.py 10

Prints the address and private key for that index, so you can import just the
account holding funds instead of clicking "Add account" in MetaMask until you
reach it.

The seed is typed in at the prompt and never written anywhere. Nothing is
saved, nothing is sent. Close the terminal when you're done.

A private key controls exactly one address. Pasting it into MetaMask
("Import account" -> Private key) gives you that account and nothing else, so
a leak costs you one address rather than the whole wallet. Still: do not put it
in a chat, a file, or a screenshot.
"""
import sys
from getpass import getpass

from bip_utils import (Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins,
                       Bip39MnemonicValidator)


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print(__doc__)
        print("Usage: python tools/derive.py <index>")
        print("  The index is one less than the MetaMask account number.")
        print("  MetaMask 'Account 10' is index 9. /wallet prints both.")
        return
    index = int(sys.argv[1])

    # getpass so the words don't stay visible on screen or in shell history
    mnemonic = getpass("Seed phrase (typing is hidden): ").strip()
    if not Bip39MnemonicValidator().IsValid(mnemonic):
        print("\nThat isn't a valid BIP39 seed phrase — check the spelling "
              "and word count.")
        return

    acct = (Bip44.FromSeed(Bip39SeedGenerator(mnemonic).Generate(),
                           Bip44Coins.ETHEREUM)
            .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)
            .AddressIndex(index))

    print(f"\n  Path        m/44'/60'/0'/0/{index}")
    print(f"  MetaMask    Account {index + 1}")
    print(f"  Address     {acct.PublicKey().ToAddress()}")
    print(f"  Private key {acct.PrivateKey().Raw().ToHex()}")
    print("\nCheck the address against /wallet before importing it.")
    print("MetaMask: account menu -> Import account -> Private key.\n")


if __name__ == "__main__":
    main()

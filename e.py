import sys
import time
import random

PARTIALS = ' ▏▎▍▌▋▊▉'

# (percent threshold, status message)
STAGES = [
    (0,   "Reading encrypted header"),
    (8,   "Detecting cipher: AES-256-CBC"),
    (15,  "Deriving key (PBKDF2, 100000 rounds)"),
    (30,  "Brute-forcing key schedule"),
    (55,  "Resolving block dependencies"),
    (75,  "Decrypting data blocks"),
    (90,  "Verifying checksum (SHA-256)"),
    (97,  "Reassembling file"),
]


def progress_bar(current, total, width=30, status="", extra=""):
    units = int(width * 8 * current / total)
    full, rem = divmod(units, 8)
    bar = '█' * full + (PARTIALS[rem] if rem else '')
    white_space = ' ' * (width - len(bar))
    percent = int((current / total) * 100)
    line = f'\r[{bar}{white_space}] {percent:3d}%  {status:<38} {extra}'
    sys.stdout.write(line.ljust(100))
    sys.stdout.flush()


def hex_chunk(n=16):
    return ''.join(random.choice('0123456789abcdef') for _ in range(n))


def current_status(pct):
    msg = STAGES[0][1]
    for threshold, text in STAGES:
        if pct >= threshold:
            msg = text
    return msg


def main():
    total = 1000
    progress = 0.0

    print("$ decrypt --file secret_data.enc --mode deep\n")
    time.sleep(0.8)

    while progress < total:
        # Uneven speed: mostly small steps, occasional bursts
        progress += random.choice([0.5, 1, 1, 2, 3, 5, 8])
        progress = min(progress, total)

        pct = progress / total * 100
        progress_bar(int(progress), total, status=current_status(pct) + "...",
                     extra=hex_chunk())

        # Random stalls make it feel real (key derivation, disk reads, etc.)
        if random.random() < 0.02:
            time.sleep(random.uniform(0.6, 1.5))
        else:
            time.sleep(random.uniform(0.01, 0.06))

    progress_bar(total, total, status="Decryption complete")
    print("\n\n[+] Integrity check passed")
    print("[+] Output written to: secret_data.dec")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Aborted by user")
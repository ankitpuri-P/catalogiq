"""Generate data/sample_listings.csv: 240 messy listings, some of them duplicates.

Deterministic (fixed seed) so the file is reproducible:
    python scripts/generate_sample.py
"""

import csv
import random
from pathlib import Path

BASE = [
    # (brand, product, size, description)
    ("AMUL", "butter", "500G", "pck of 2"), ("amul", "taaza toned milk", "1L", "tetra pack"),
    ("Tata", "salt iodised", "1kg", ""), ("Aashirvaad", "whole wheat atta", "5 KG", "100% MP wheat"),
    ("Fortune", "sunflower oil", "1 ltr", "refined, pouch"), ("Britannia", "good day cashew biscuit", "200g", ""),
    ("Parle", "g biscuit", "800 gm", "family pack"), ("MAGGI", "2-minute masala noodles", "70g", "pack of 12"),
    ("Haldiram's", "aloo bhujia namkeen", "400g", ""), ("Lays", "classic salted chips", "52g", ""),
    ("Cadbury", "dairy milk silk chocolate", "150g", ""), ("Kissan", "fresh tomato ketchup", "950g", "bottle"),
    ("Saffola", "gold edible oil", "5L", "jar"), ("Mother Dairy", "classic curd", "400g", "cup"),
    ("Patanjali", "cow ghee", "1 L", ""), ("MDH", "garam masala", "100g", "spice mix"),
    ("Everest", "kitchen king masala", "100 G", ""), ("Kellogg's", "corn flakes original", "875g", ""),
    ("Quaker", "oats", "1kg", "rolled oats"), ("Dabur", "honey", "500 g", "100% pure"),
    ("Tata", "tea premium", "1kg", "strong chai"), ("Red Label", "tea", "500g", ""),
    ("Bru", "instant coffee", "200g", "jar"), ("Nescafe", "classic coffee", "100 g", ""),
    ("Coca-Cola", "soft drink", "2.25 L", "bottle"), ("Pepsi", "cola can", "300ml", "pack of 6"),
    ("Red Bull", "energy drink", "250 ml", "can"), ("Real", "mixed fruit juice", "1L", ""),
    ("Tropicana", "orange juice 100%", "1 ltr", "no added sugar"), ("Bisleri", "mineral water", "1L", "pack of 12"),
    ("Paper Boat", "aam panna", "200ml", ""), ("Horlicks", "health drink", "500g", "chocolate flavour"),
    ("Colgate", "strong teeth toothpaste", "200g", "pack of 2"), ("Pepsodent", "toothbrush soft", "", "pack of 4"),
    ("Dove", "cream beauty bathing bar soap", "100g", "pack of 3"), ("Lux", "soft skin soap", "150 g", ""),
    ("Lifebuoy", "hand sanitizer", "500ml", ""), ("Dettol", "original liquid handwash", "200ml", "refill"),
    ("Himalaya", "neem face wash", "150ml", ""), ("Nivea", "soft light moisturising cream", "100ml", ""),
    ("Head & Shoulders", "anti dandruff shampoo", "340 ml", ""), ("Parachute", "coconut hair oil", "500ml", ""),
    ("Gillette", "mach3 razor", "", "with 2 cartridges"), ("Mamaearth", "onion hair oil", "250 ml", ""),
    ("Vaseline", "lip balm", "10g", "rosy lips"), ("Surf Excel", "easy wash detergent powder", "1.5kg", ""),
    ("Ariel", "matic front load detergent", "2 kg", ""), ("Tide", "plus detergent powder", "1kg", ""),
    ("Vim", "dishwash liquid gel", "750 ml", "lemon"), ("Harpic", "toilet cleaner", "1 L", "pack of 2"),
    ("Lizol", "floor cleaner citrus", "975ml", ""), ("Good Knight", "gold flash refill", "", "insect repellent"),
    ("Odonil", "air freshener blocks", "50g", "pack of 4"), ("", "garbage bag medium", "", "30 bags, black"),
    ("", "kitchen tissue roll", "", "pack of 2"), ("Boat", "airdopes 141 bluetooth earbuds", "", "black"),
    ("Noise", "colorfit smartwatch", "", "1.8 inch display"), ("Mi", "power bank 20000mah", "", "fast charging"),
    ("Samsung", "25w usb-c charger", "", "adapter only"), ("JBL", "go 3 bluetooth speaker", "", "blue"),
    ("Philips", "led bulb 9w", "", "cool day light, pack of 4"), ("Syska", "usb c cable 1m", "", ""),
    ("", "wireless mouse", "", "2.4ghz, usb receiver"), ("", "aa battery", "", "pack of 10"),
    ("Levi's", "men slim fit jeans", "", "blue, size 32"), ("Puma", "men running shoe", "", "size 9"),
    ("Nike", "dri-fit t-shirt", "", "black, M"), ("Bata", "women sandal", "", "size 6"),
    ("", "cotton kurta women", "", "printed, size L"), ("", "ankle socks", "", "pack of 5"),
    ("Red Tape", "leather belt men", "", "brown"), ("", "denim jacket unisex", "", ""),
    ("Prestige", "pressure cooker", "3 L", "aluminium"), ("Pigeon", "non stick tawa", "", "28 cm"),
    ("Milton", "thermosteel water bottle", "1L", "hot & cold"), ("Cello", "plastic container set", "", "set of 6"),
    ("Borosil", "glass mug", "350ml", "pack of 2"), ("Hawkins", "stainless steel kadai", "2.5 L", ""),
    ("", "cotton bath towel", "", "set of 2"), ("", "double bedsheet with pillow covers", "", "floral"),
    ("", "kitchen knife set", "", "3 pcs"), ("", "steel lunch box", "", "3 containers"),
    ("", "fresh paneer", "200g", "malai"), ("", "brown bread", "400 g", ""),
    ("", "farm eggs", "", "pack of 12"), ("", "basmati rice", "5kg", "long grain"),
    ("", "toor dal", "1 kg", "unpolished"), ("", "sugar", "1kg", ""),
    ("", "gift card holder", "", ""), ("", "yoga mat", "", "6mm, anti slip"),
    ("", "notebook a4 ruled", "", "pack of 6"), ("", "pet food dog adult", "3 kg", "chicken"),
]

SELLER_NOISE = ["", "", "", " BEST PRICE", " !!", " - new", " (combo)", " offer"]


def messy(brand, product, size, rng):
    parts = [brand, product, size]
    parts = [p for p in parts if p]
    text = "  ".join(parts) if rng.random() < 0.3 else " ".join(parts)
    style = rng.random()
    if style < 0.25:
        text = text.upper()
    elif style < 0.45:
        text = text.lower()
    return (" " if rng.random() < 0.2 else "") + text + rng.choice(SELLER_NOISE)


def main():
    rng = random.Random(42)
    rows = []
    sku = 1000
    for _ in range(2):  # two passes over the catalogue with different noise -> 184 rows
        for brand, product, size, desc in BASE:
            sku += 1
            rows.append((f"SKU{sku}", messy(brand, product, size, rng), desc))
    # Exact-content duplicates: same text, different SKU, different case/spacing.
    for i in rng.sample(range(len(rows)), 50):
        sku += 1
        _, title, desc = rows[i]
        variant = rng.choice([title.upper(), title.lower(), "  " + title.replace(" ", "   ")])
        rows.append((f"SKU{sku}", variant, desc))
    rng.shuffle(rows)

    out = Path(__file__).resolve().parent.parent / "data" / "sample_listings.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sku", "raw_title", "raw_description"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()

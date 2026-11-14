"""
Name generator: a neural network built from scratch (NumPy only, manual backprop).

How it works:
  - Each name is split into characters, with "." marking start/end of a name.
  - The network looks at the previous CONTEXT characters (one-hot encoded)
    and predicts a probability for every possible next character.
  - Training = nudging the weights so the real next character gets higher probability.
  - Generating = start with "...", sample next characters until "." appears.

Run:  python name_generator.py          -> trains, then prints 10 names
      python name_generator.py 25       -> prints 25 names
Add more names to NAMES to improve variety and quality.
"""
import sys
import cupy as np

NAMES = """
danica carl emma olivia noah liam ava sophia isabella mia charlotte amelia harper evelyn abigail
emily ella elizabeth camila luna sofia avery mila aria scarlett penelope layla chloe victoria madison
eleanor grace nora riley zoey hannah hazel lily ellie violet lillian zoe stella aurora natalie emilia
everly leah aubrey willow addison lucy audrey claire brooklyn bella skylar genesis naomi eliana
maya anna kennedy ruby sadie alice eva josephine cora lydia vivian delilah ivy clara adeline
james oliver benjamin elijah lucas mason logan alexander ethan jacob michael daniel henry jackson
sebastian aiden matthew samuel david joseph carter owen wyatt john jack luke jayden dylan grayson
levi isaac gabriel julian mateo anthony jaxon lincoln joshua christopher andrew theodore caleb ryan
asher nathan thomas leo isaiah charles josiah hudson christian hunter connor eli ezra aaron landon
adrian jonathan nolan jeremiah easton elias colton cameron carson robert angel maverick nicholas
dominic jaxson greyson adam ian austin santiago jordan cooper brayden roman evan ezekiel xavier
vincent miles damian sawyer jason bryce kai harrison marcus felix oscar victor edward george
frederick arthur walter peter martin simon patrick stephen andre bruno diego rafael tomas hugo
sara laura marta paula elena julia carmen sofia irene diana rosa lola alba nuria teresa pilar
maria jose luis juan pedro miguel carlos jorge pablo sergio alberto fernando ricardo eduardo
mario enrique alejandro ramon rodrigo gonzalo adriana beatriz catalina daniela fernanda gabriela
helena ines jimena karina lucia monica natalia patricia raquel silvia tamara valentina vanessa
donna dolores dorothy deborah debra denise diane dawn danielle daisy dana dina dora
carla carol carolyn caroline cassandra catherine cecilia celeste charlene cheryl christina cindy
""".split()

CONTEXT = 4       # how many previous characters the network sees
HIDDEN = 256      # neurons in the hidden layer
EPOCHS = 400
LR = 0.1          # learning rate
SEED = None       # set to an int for reproducible results

rng = np.random.default_rng(SEED)

# ---------- vocabulary ----------
chars = sorted(set("".join(NAMES)))
stoi = {c: i + 1 for i, c in enumerate(chars)}
stoi["."] = 0
itos = {i: c for c, i in stoi.items()}
V = len(stoi)

# ---------- dataset ----------
def build_dataset(names):
    X, Y = [], []
    for name in names:
        ctx = [0] * CONTEXT
        for ch in name + ".":
            X.append(ctx.copy())
            Y.append(stoi[ch])
            ctx = ctx[1:] + [stoi[ch]]
    return np.array(X), np.array(Y)

def one_hot(X):
    """(N, CONTEXT) ints -> (N, CONTEXT*V) one-hot floats."""
    out = np.zeros((X.shape[0], CONTEXT * V))
    for pos in range(CONTEXT):
        out[np.arange(X.shape[0]), pos * V + X[:, pos]] = 1.0
    return out

# ---------- the network ----------
class NeuralNet:
    def __init__(self, n_in, n_hidden, n_out):
        self.W1 = rng.standard_normal((n_in, n_hidden)) * np.sqrt(1 / n_in)
        self.b1 = np.zeros(n_hidden)
        self.W2 = rng.standard_normal((n_hidden, n_out)) * np.sqrt(1 / n_hidden)
        self.b2 = np.zeros(n_out)

    def forward(self, x):
        self.x = x
        self.h = np.tanh(x @ self.W1 + self.b1)          # hidden layer
        logits = self.h @ self.W2 + self.b2              # output layer
        logits -= logits.max(axis=1, keepdims=True)      # numerical stability
        exp = np.exp(logits)
        self.probs = exp / exp.sum(axis=1, keepdims=True)  # softmax
        return self.probs

    def loss(self, y):
        n = len(y)
        return -np.log(self.probs[np.arange(n), y] + 1e-9).mean()

    def backward(self, y, lr):
        n = len(y)
        d_logits = self.probs.copy()
        d_logits[np.arange(n), y] -= 1
        d_logits /= n
        dW2 = self.h.T @ d_logits
        db2 = d_logits.sum(axis=0)
        d_h = d_logits @ self.W2.T
        d_pre = d_h * (1 - self.h ** 2)                  # tanh derivative
        dW1 = self.x.T @ d_pre
        db1 = d_pre.sum(axis=0)
        self.W1 -= lr * dW1
        self.b1 -= lr * db1
        self.W2 -= lr * dW2
        self.b2 -= lr * db2

# ---------- training ----------
def train(net, X, Y, epochs=EPOCHS, batch=256):
    Xoh = one_hot(X)
    n = len(Y)
    for epoch in range(1, epochs + 1):
        idx = rng.permutation(n)
        total = 0.0
        for i in range(0, n, batch):
            b = idx[i:i + batch]
            net.forward(Xoh[b])
            total += net.loss(Y[b]) * len(b)
            net.backward(Y[b], LR)
        if epoch % 50 == 0 or epoch == 1:
            print(f"epoch {epoch:4d}  loss {total / n:.4f}")

# ---------- generation ----------
def generate(net, temperature=0.9, min_len=3, max_len=9):
    while True:
        ctx = [0] * CONTEXT
        out = []
        while len(out) < max_len:
            p = net.forward(one_hot(np.array([ctx])))[0]
            p = p ** (1 / temperature)
            if len(out) < min_len:
                p[0] = 0                                  # forbid ending too early
            p /= p.sum()
            i = rng.choice(V, p=p)
            if i == 0:
                break
            out.append(itos[i])
            ctx = ctx[1:] + [i]
        name = "".join(out)
        if min_len <= len(name) <= max_len and name not in NAMES:  # skip memorized names
            return name.capitalize()

if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    X, Y = build_dataset(NAMES)
    net = NeuralNet(CONTEXT * V, HIDDEN, V)
    print(f"Training on {len(NAMES)} names ({len(Y)} samples)...")
    train(net, X, Y)
    print("\nGenerated names:")
    for _ in range(count):
        print(" ", generate(net))
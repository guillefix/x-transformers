import math
import torch
from torch import nn, einsum
import torch.nn.functional as F
from functools import partial
from inspect import isfunction
from einops import rearrange, repeat

from x_transformers.autoregressive_wrapper import AutoregressiveWrapper

# helpers

def exists(val):
    return val is not None

def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d

# positional embeddings

class RelativePositionBias(nn.Module):
    def __init__(self, causal = False, num_buckets = 32, max_distance = 128, heads = 8):
        super().__init__()
        self.causal = causal
        self.num_buckets = num_buckets
        self.max_distance = max_distance
        self.relative_attention_bias = nn.Embedding(num_buckets, heads)

    @staticmethod
    def _relative_position_bucket(relative_position, causal = True, num_buckets = 32, max_distance = 128):
        ret = 0
        n = -relative_position
        if causal:
            num_buckets //= 2
            ret += (n < 0).long() * num_buckets
            n = torch.abs(n)
        else:
            n = torch.max(n, torch.zeros_like(n))

        max_exact = num_buckets // 2
        is_small = n < max_exact

        val_if_large = max_exact + (
            torch.log(n.float() / max_exact) / math.log(max_distance / max_exact) * (num_buckets - max_exact)
        ).long()
        val_if_large = torch.min(val_if_large, torch.full_like(val_if_large, num_buckets - 1))

        ret += torch.where(is_small, n, val_if_large)
        return ret

    def forward(self, qk_dots):
        i, j, device = *qk_dots.shape[-2:], qk_dots.device
        q_pos = torch.arange(i, dtype = torch.long, device = device)
        k_pos = torch.arange(j, dtype = torch.long, device = device)
        rel_pos = k_pos[None, :] - q_pos[:, None]
        rp_bucket = self._relative_position_bucket(rel_pos, causal = self.causal, num_buckets = self.num_buckets)
        values = self.relative_attention_bias(rp_bucket)
        bias = rearrange(values, 'i j h -> () h i j')
        return qk_dots + bias

# classes

class ScaleNorm(nn.Module):
    def __init__(self, dim, eps = 1e-5):
        super().__init__()
        self.eps = eps
        self.g = nn.Parameter(torch.ones(1))

    def forward(self, x, **kwargs):
        n = torch.norm(x, dim = -1, keepdim = True).clamp(min = self.eps)
        return x / n * self.g

class PreNorm(nn.Module):
    def __init__(self, dim, fn, norm_class = nn.LayerNorm):
        super().__init__()
        self.fn = fn
        self.norm = norm_class(dim)

    def forward(self, x, **kwargs):
        x = self.norm(x)
        return self.fn(x, **kwargs)

class GEGLU(nn.Module):
    def __init__(self, dim_in, dim_out):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2)

    def forward(self, x):
        x, gate = self.proj(x).chunk(2, dim = -1)
        return x * F.gelu(x)

class FeedForward(nn.Module):
    def __init__(self, dim, dim_out = None, mult = 4, glu = False, dropout = 0.):
        super().__init__()
        dim_out = default(dim_out, dim)
        project_in = nn.Sequential(
            nn.Linear(dim, dim * mult),
            nn.GELU()
        ) if not glu else GEGLU(dim, dim * mult)

        self.net = nn.Sequential(
            project_in,
            nn.Dropout(dropout),
            nn.Linear(dim * mult, dim_out)
        )

    def forward(self, x):
        return self.net(x)

class Attention(nn.Module):
    def __init__(self, dim, dim_head = 64, heads = 8, causal = False, mask = None):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        self.causal = causal
        self.mask = mask

        inner_dim = dim_head * heads
        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim)

    def forward(self, x, context = None, mask = None, context_mask = None, rel_pos = None):
        b, n, _, h, device = *x.shape, self.heads, x.device
        kv_input = default(context, x)

        q = self.to_q(x)
        kv = self.to_kv(kv_input).chunk(2, dim = -1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, *kv))
        dots = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale

        if exists(rel_pos):
            dots = rel_pos(dots)

        if any(map(exists, (mask, context_mask))):
            q_mask = default(mask, lambda: torch.ones((b, dots.shape[-2]), device = device).bool())
            k_mask = default(context_mask, lambda: torch.ones((b, dots.shape[-1]), device = device).bool())
            q_mask = rearrange(q_mask, 'b i -> b () i ()')
            k_mask = rearrange(k_mask, 'b j -> b () () j')
            mask = q_mask * k_mask
            dots.masked_fill_(mask, float('-inf'))
            del mask

        if self.causal:
            mask = torch.ones((n, n), device = device).triu_(1).bool()
            dots.masked_fill_(mask, float('-inf'))
            del mask

        attn = dots.softmax(dim = -1)
        out = einsum('b h i j, b h j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

class Encoder(nn.Module):
    def __init__(self, dim, depth, dim_head = 64, heads = 8, use_scalenorm = False, ff_glu = False, rel_pos_bias = False):
        super().__init__()
        self.dim = dim
        self.layers = nn.ModuleList([])
        self.rel_pos = RelativePositionBias(causal = True) if rel_pos_bias else None

        norm_class = ScaleNorm if use_scalenorm else nn.LayerNorm
        prenorm_fn = partial(PreNorm, dim, norm_class = norm_class)

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                prenorm_fn(Attention(dim, dim_head = dim_head, heads = heads)),
                prenorm_fn(FeedForward(dim, glu = ff_glu))
            ]))
    def forward(self, x, context = None, mask = None):
        for (self_attn, ff) in self.layers:
            x = self_attn(x, mask = mask, rel_pos = self.rel_pos) + x
            x = ff(x) + x
        return x

class Decoder(nn.Module):
    def __init__(self, dim, depth, dim_head = 64, heads = 8, cross_attend = False, use_scalenorm = False, ff_glu = False, rel_pos_bias = False):
        super().__init__()
        self.dim = dim
        self.layers = nn.ModuleList([])
        self.rel_pos = RelativePositionBias(causal = True) if rel_pos_bias else None

        norm_class = ScaleNorm if use_scalenorm else nn.LayerNorm
        prenorm_fn = partial(PreNorm, dim, norm_class = norm_class)

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                prenorm_fn(Attention(dim, dim_head = dim_head, heads = heads, causal = True)),
                prenorm_fn(Attention(dim, dim_head = dim_head, heads = heads)) if cross_attend else None,
                prenorm_fn(FeedForward(dim, glu = ff_glu)),
            ]))
    def forward(self, x, context = None, mask = None, context_mask = None):
        for (self_attn, cross_attn, ff) in self.layers:
            x = self_attn(x, rel_pos = self.rel_pos) + x
            if exists(cross_attn):
                x = cross_attn(x, context = context, mask = mask, context_mask = context_mask) + x
            x = ff(x) + x
        return x

class ViTransformerWrapper(nn.Module):
    def __init__(
        self,
        *,
        image_size,
        patch_size,
        attn_layers,
        num_classes = None,
        dropout = 0.,
        emb_dropout = 0.
    ):
        super().__init__()
        assert image_size % patch_size == 0, 'image dimensions must be divisible by the patch size'
        dim = attn_layers.dim
        num_patches = (image_size // patch_size) ** 2
        patch_dim = 3 * patch_size ** 2

        self.patch_size = patch_size

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim))
        self.patch_to_embedding = nn.Linear(patch_dim, dim)
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))
        self.dropout = nn.Dropout(emb_dropout)

        self.attn_layers = attn_layers
        self.norm = nn.LayerNorm(dim)
        self.mlp_head = FeedForward(dim, dim_out = num_classes, dropout = dropout) if exists(num_classes) else None

    def forward(self, img):
        p = self.patch_size

        x = rearrange(img, 'b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = p, p2 = p)
        x = self.patch_to_embedding(x)
        b, n, _ = x.shape

        cls_tokens = repeat(self.cls_token, '() n d -> b n d', b = b)
        x = torch.cat((cls_tokens, x), dim=1)
        x += self.pos_embedding[:, :(n + 1)]
        x = self.dropout(x)

        x = self.attn_layers(x)
        x = self.norm(x)

        if not exists(self.mlp_head):
            return x

        return self.mlp_head(x[:, 0])

class TransformerWrapper(nn.Module):
    def __init__(
        self,
        *,
        num_tokens,
        max_seq_len,
        attn_layers
    ):
        super().__init__()
        dim = attn_layers.dim
        self.max_seq_len = max_seq_len
        self.token_emb = nn.Embedding(num_tokens, dim)
        self.pos_emb = nn.Embedding(max_seq_len, dim)
        self.attn_layers = attn_layers
        self.norm = nn.LayerNorm(dim)

        self.init_()
        self.to_logits = lambda t: t @ self.token_emb.weight.t()

    def init_(self):
        nn.init.normal_(self.token_emb.weight, std = 0.02)
        nn.init.normal_(self.pos_emb.weight, std = 0.02)

    def forward(self, x, return_embeddings = False, **kwargs):
        _, n, device = *x.shape, x.device
        x = self.token_emb(x)
        x += self.pos_emb(torch.arange(n, device = device))
        x = self.attn_layers(x, **kwargs)
        x = self.norm(x)

        if return_embeddings:
            return x
        return self.to_logits(x)

class XTransformer(nn.Module):
    def __init__(
        self,
        *,
        num_tokens,
        dim,
        depth,
        max_seq_len,
        heads = 8,
        return_tgt_loss = False
    ):
        super().__init__()

        self.encoder = TransformerWrapper(
            num_tokens = num_tokens,
            max_seq_len = max_seq_len,
            attn_layers = Encoder(dim, depth, heads)
        )

        self.decoder = TransformerWrapper(
            num_tokens = num_tokens,
            max_seq_len = max_seq_len,
            attn_layers = Decoder(dim, depth, heads, cross_attend = True)
        )

        if return_tgt_loss:
            self.decoder = AutoregressiveWrapper(self.decoder)

    def forward(self, src, tgt, src_mask = None, tgt_mask = None):
        enc = self.encoder(src, mask = src_mask, return_embeddings = True)
        out = self.decoder(tgt, context = enc, mask = tgt_mask, context_mask = src_mask)
        return out

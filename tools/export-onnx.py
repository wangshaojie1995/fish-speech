import torch
import torch.nn.functional as F
from einx import get_at

from fish_speech.conversation import CODEBOOK_PAD_TOKEN_ID
from tools.vqgan.extract_vq import get_model

PAD_TOKEN_ID = torch.LongTensor([CODEBOOK_PAD_TOKEN_ID])


class Encoder(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.model.spec_transform.spectrogram.return_complex = False

    def forward(self, audios):
        mels = self.model.spec_transform(audios)
        encoded_features = self.model.backbone(mels)
        indices = self.model.quantizer.encode(encoded_features)
        return indices


class Decoder(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.model.head.training = False
        self.model.head.checkpointing = False

    def get_codes_from_indices(self, cur_index, indices):

        batch_size, quantize_dim, q_dim = indices.shape
        d_dim = self.model.quantizer.residual_fsq.rvqs[cur_index].codebooks.shape[2]

        # because of quantize dropout, one can pass in indices that are coarse
        # and the network should be able to reconstruct

        if (
            quantize_dim
            < self.model.quantizer.residual_fsq.rvqs[cur_index].num_quantizers
        ):
            assert (
                self.model.quantizer.residual_fsq.rvqs[cur_index].quantize_dropout > 0.0
            ), "quantize dropout must be greater than 0 if you wish to reconstruct from a signal with less fine quantizations"
            indices = F.pad(
                indices,
                (
                    0,
                    self.model.quantizer.residual_fsq.rvqs[cur_index].num_quantizers
                    - quantize_dim,
                ),
                value=-1,
            )

        # take care of quantizer dropout

        mask = indices == -1
        indices = indices.masked_fill(
            mask, 0
        )  # have it fetch a dummy code to be masked out later

        all_codes = torch.gather(
            self.model.quantizer.residual_fsq.rvqs[cur_index].codebooks.unsqueeze(1),
            dim=2,
            index=indices.long()
            .permute(2, 0, 1)
            .unsqueeze(-1)
            .repeat(1, 1, 1, d_dim),  # q, batch_size, frame, dim
        )

        all_codes = all_codes.masked_fill(mask.permute(2, 0, 1).unsqueeze(-1), 0.0)

        # scale the codes

        scales = (
            self.model.quantizer.residual_fsq.rvqs[cur_index]
            .scales.unsqueeze(1)
            .unsqueeze(1)
        )
        all_codes = all_codes * scales

        # if (accept_image_fmap = True) then return shape (quantize, batch, height, width, dimension)

        return all_codes

    def get_output_from_indices(self, cur_index, indices):
        codes = self.get_codes_from_indices(cur_index, indices)
        codes_summed = codes.sum(dim=0)
        return self.model.quantizer.residual_fsq.rvqs[cur_index].project_out(
            codes_summed
        )

    def forward(self, indices) -> torch.Tensor:
        batch_size, _, length = indices.shape
        dims = self.model.quantizer.residual_fsq.dim
        groups = self.model.quantizer.residual_fsq.groups
        dim_per_group = dims // groups

        # indices = rearrange(indices, "b (g r) l -> g b l r", g=groups)
        indices = indices.view(batch_size, groups, -1, length).permute(1, 0, 3, 2)

        # z_q = self.model.quantizer.residual_fsq.get_output_from_indices(indices)
        z_q = torch.empty((batch_size, length, dims))
        for i in range(groups):
            z_q[:, :, i * dim_per_group : (i + 1) * dim_per_group] = (
                self.get_output_from_indices(i, indices[i])
            )

        z = self.model.quantizer.upsample(z_q.transpose(1, 2))
        x = self.model.head(z)
        return x


def main():
    GanModel = get_model(
        "firefly_gan_vq",
        "checkpoints/pre/firefly-gan-vq-fsq-8x1024-21hz-generator.pth",
        device="cpu",
    )
    enc = Encoder(GanModel)
    dec = Decoder(GanModel)
    audio_example = torch.randn(1, 1, 96000)
    indices = enc(audio_example)

    print(dec(indices).shape)

    """
    torch.onnx.export(
        enc,
        audio_example,
        "encoder.onnx",
        dynamic_axes = {
            "audio": [0, 2],
        },
        do_constant_folding=False,
        opset_version=18,
        verbose=False,
        input_names=["audio"],
        output_names=["prompt"]
    )
    """

    torch.onnx.export(
        dec,
        indices,
        "decoder.onnx",
        dynamic_axes={
            "prompt": [0, 2],
        },
        do_constant_folding=False,
        opset_version=18,
        verbose=False,
        input_names=["prompt"],
        output_names=["audio"],
    )

    print(enc(audio_example).shape)
    print(dec(enc(audio_example)).shape)


main()

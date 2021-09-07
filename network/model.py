import torch
import torch.nn as nn
import torch.nn.functional as F
from utils.utils import get_model, read_json
from .layers import AttentionLayer, BiaffineAttention

class BERTModel(nn.Module):

    def __init__(self, model_config, norm_labels, punc_labels, hidden_dim, model_mode):
        super().__init__()
        self.bert = get_model(model_config)
        self.attn = AttentionLayer(self.bert.config.hidden_size, hidden_dim)
        mlp_dim = self.attn.output_dim // 2
        self.n_norm_labels = len(norm_labels)
        self.n_punc_labels = len(punc_labels)
        self.mode = model_mode
        
        self.using_biaffine = True

        self.norm_mlp = nn.Linear(self.attn.output_dim, mlp_dim)
        self.punc_mlp = nn.Linear(self.attn.output_dim, mlp_dim)

        if self.using_biaffine:
            self.norm_decoder = BiaffineAttention(mlp_dim, self.n_norm_labels)
            self.punc_decoder = BiaffineAttention(mlp_dim, self.n_punc_labels)
        else:
            self.norm_decoder = nn.Linear(mlp_dim, self.n_norm_labels)
            self.punc_decoder = nn.Linear(mlp_dim, self.n_punc_labels)
        
        self.norm_criterion = nn.CrossEntropyLoss()
        self.punc_criterion = nn.CrossEntropyLoss()
    
    @classmethod
    def from_config(cls, model_config, norm_labels, punc_labels, hidden_dim, model_mode):
        model_config = read_json(model_config)["pretrained_model"]
        return cls(model_config, norm_labels, punc_labels, hidden_dim, model_mode)

    def forward_encoders(self, input_ids, mask_ids, next_blocks=None, prev_blocks=None):
        bert_output = self.bert(input_ids, mask_ids)[0]
        if next_blocks is not None and prev_blocks is not None:
            with torch.no_grad():
                next_blocks = [self.bert(block)[0] for block in next_blocks]
                prev_blocks = [self.bert(block)[0] for block in prev_blocks]
                next_blocks = torch.cat(next_blocks, 1)
                prev_blocks = torch.cat(prev_blocks, 1)
            bert_output = torch.cat((prev_blocks, bert_output, next_blocks), 1)
        return bert_output, next_blocks, prev_blocks
    
    def forward_hidden_layers(self, bert_output, next_blocks, prev_blocks):
        hidden_output, _ = self.attn(bert_output)
        if next_blocks is not None and prev_blocks is not None:
            next_dim = next_blocks.shape[1]
            prev_dim = prev_blocks.shape[1]
            hidden_output = hidden_output[:,prev_dim:-next_dim,:].contiguous()
        hidden_output = torch.tanh(hidden_output)
        return hidden_output
    
    def set_bert_mode(self, mode):
        if mode == "encoder":
            self.bert.config.is_decoder = False
            self.bert.config.add_cross_attention = False
        elif mode == "decoder":
            self.bert.config.is_decoder = True
            self.bert.config.add_cross_attention = True
    
    def forward_bert(self, input_ids, mask_ids, encoder_hidden_states=None):
        self.set_bert_mode("encoder" if encoder_hidden_states is None else "decoder")
        bert_emb = self.bert.embeddings(input_ids)
        extended_attention_mask = self.bert.get_extended_attention_mask(mask_ids, input_ids.size(), input_ids.device)
        hidden_states = self.bert.encoder(bert_emb, attention_mask=extended_attention_mask, encoder_hidden_states=encoder_hidden_states)
        return hidden_states

    def forward(self, input_ids, mask_ids, norm_ids=None, punc_ids=None, next_blocks=None, prev_blocks=None):
        if self.mode == "nojoint":
            norm_bert_output = self.forward_bert(input_ids, mask_ids)[0]
            punc_bert_output = self.forward_bert(input_ids, mask_ids)[0]
        elif self.mode == "norm_to_punc":
            norm_bert_output = self.forward_bert(input_ids, mask_ids)[0]
            punc_bert_output = self.forward_bert(input_ids, mask_ids, norm_bert_output)[0]
        elif self.mode == "punc_to_norm":
            punc_bert_output = self.forward_bert(input_ids, mask_ids)[0]
            norm_bert_output = self.forward_bert(input_ids, mask_ids, punc_bert_output)[0]
        
        if next_blocks is not None and prev_blocks is not None:
            with torch.no_grad():
                next_blocks = [self.bert(block)[0] for block in next_blocks]
                prev_blocks = [self.bert(block)[0] for block in prev_blocks]
                next_blocks = torch.cat(next_blocks, 1)
                prev_blocks = torch.cat(prev_blocks, 1)
            norm_bert_output = torch.cat((prev_blocks, norm_bert_output, next_blocks), 1)
            punc_bert_output = torch.cat((prev_blocks, punc_bert_output, next_blocks), 1)
        
        norm_hidden_output = self.forward_hidden_layers(norm_bert_output, next_blocks, prev_blocks)
        punc_hidden_output = self.forward_hidden_layers(punc_bert_output, next_blocks, prev_blocks)

        norm_mlp_output = torch.tanh(self.norm_mlp(norm_hidden_output))
        punc_mlp_output = torch.tanh(self.punc_mlp(punc_hidden_output))

        if self.using_biaffine:
            norm_logits = self.norm_decoder(norm_mlp_output, punc_mlp_output)
            punc_logits = self.punc_decoder(punc_mlp_output, norm_mlp_output)
        else:
            norm_logits = self.norm_decoder(norm_mlp_output)
            punc_logits = self.punc_decoder(punc_mlp_output)

        if norm_ids is None and punc_ids is None:
            return norm_logits, punc_logits
        else:
            norm_ids = norm_ids.view(-1)
            punc_ids = punc_ids.view(-1)
            norm_loss = self.norm_criterion(norm_logits.view(norm_ids.shape[0], -1), norm_ids)
            punc_loss = self.punc_criterion(punc_logits.view(punc_ids.shape[0], -1), punc_ids)
            return norm_loss, punc_loss
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from typing import List, Dict
from analysis.uncertainty import compute_uncertainty_scores  # Adjust if your path is different

_model_cache = {}
_tokenizer_cache = {}

def load_model_and_tokenizer(model_name: str):
    if model_name not in _model_cache:
        tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False)
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.float16,
            device_map="auto",
            trust_remote_code=True
        )
        model.eval()
        _model_cache[model_name] = model
        _tokenizer_cache[model_name] = tokenizer
    return _model_cache[model_name], _tokenizer_cache[model_name]

def run_generation(
    batch: List[Dict],
    model_name: str,
    return_logits: bool = True,
    return_attentions: bool = False,
    uncertainty_methods: List[str] = None
) -> List[Dict]:

    model, tokenizer = load_model_and_tokenizer(model_name)

    # Prepare the prompts
    prompts = [sample.get("prompt", f"Question: {sample['question']} Answer:") for sample in batch]
    encoded = tokenizer(prompts, return_tensors="pt", padding=True).to(model.device)

    with torch.no_grad():
        outputs = model.generate(
            **encoded,
            max_length=encoded["input_ids"].shape[1] + 64,
            do_sample=False,
            return_dict_in_generate=True,
            output_scores=return_logits,
            output_attentions=return_attentions
        )

    generated = outputs.sequences
    decoded = tokenizer.batch_decode(generated[:, encoded["input_ids"].shape[1]:], skip_special_tokens=True)

    result = []
    for i, sample in enumerate(batch):
        item = {
            "input_ids": encoded["input_ids"][i],
            "generated_ids": generated[i],
            "generated_text": decoded[i],
        }

        if return_logits:
            item["scores"] = outputs.scores  # list of [batch, vocab] scores per generated token

        if return_attentions and hasattr(outputs, 'attentions'):
            attentions = outputs.attentions
            log_attention = []
            for attention in attentions:
                if isinstance(attention, torch.Tensor):
                    attention = torch.clamp(attention, min=1e-10)
                    log_attention.append(torch.log(1 + attention))
                else:
                    attention_tensor = torch.clamp(attention[0], min=1e-10)
                    log_attention.append(torch.log(1 + attention_tensor))
            item["log_attentions"] = log_attention

        # Add context for uncertainty score computation
        likelihood_dict = {
            "token_log_likelihoods": sample.get("token_log_likelihoods", torch.tensor([])),
            "entropy_per_token": sample.get("entropy_per_token", torch.tensor([])),
            "logits": outputs.scores if return_logits else None
        }
        model_output = {
            "generated_text": decoded[i],
            "input_text": prompts[i],
            "log_attentions": item.get("log_attentions", None)
        }

        # Compute uncertainty scores if requested
        if uncertainty_methods:
            try:
                scores = compute_uncertainty_scores(likelihood_dict, model_output, methods=uncertainty_methods)
                item["uncertainty_scores"] = scores
            except Exception as e:
                print(f"⚠️ Failed to compute uncertainty scores for sample {i}: {e}")
                item["uncertainty_scores"] = {}

        result.append(item)

    return result

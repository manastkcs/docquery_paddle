# NOTE: This code is currently under review for inclusion in the main
# huggingface/transformers repository:
# https://github.com/huggingface/transformers/pull/18414
import re
from typing import List, Optional, Tuple, Union

import numpy as np
from transformers.pipelines.base import PIPELINE_INIT_ARGS, ChunkPipeline
from transformers.utils import (
    ExplicitEnum,
    add_end_docstrings,
    is_pytesseract_available,
    is_torch_available,
    is_vision_available,
    logging,
)

from .itertools import unique_everseen
from .qa_helpers import TESSERACT_LOADED, VISION_LOADED, Image, load_image, pytesseract, select_starts_ends


if is_torch_available():
    import torch

    # We do not perform the check in this version of the pipeline code
    # from transformers.models.auto.modeling_auto import MODEL_FOR_DOCUMENT_QUESTION_ANSWERING_MAPPING

logger = logging.get_logger(__name__)


# normalize_bbox() and apply_tesseract() are derived from apply_tesseract in models/layoutlmv3/feature_extraction_layoutlmv3.py.
# However, because the pipeline may evolve from what layoutlmv3 currently does, it's copied (vs. imported) to avoid creating an
# unnecessary dependency.
def normalize_box(box, width, height):
    return [
        int(1000 * (box[0] / width)),
        int(1000 * (box[1] / height)),
        int(1000 * (box[2] / width)),
        int(1000 * (box[3] / height)),
    ]


def apply_tesseract(image: "Image.Image", lang: Optional[str], tesseract_config: Optional[str]):
    """Applies Tesseract OCR on a document image, and returns recognized words + normalized bounding boxes."""
    # apply OCR
    data = pytesseract.image_to_data(image, lang=lang, output_type="dict", config=tesseract_config)
    words, left, top, width, height = data["text"], data["left"], data["top"], data["width"], data["height"]

    # filter empty words and corresponding coordinates
    irrelevant_indices = [idx for idx, word in enumerate(words) if not word.strip()]
    words = [word for idx, word in enumerate(words) if idx not in irrelevant_indices]
    left = [coord for idx, coord in enumerate(left) if idx not in irrelevant_indices]
    top = [coord for idx, coord in enumerate(top) if idx not in irrelevant_indices]
    width = [coord for idx, coord in enumerate(width) if idx not in irrelevant_indices]
    height = [coord for idx, coord in enumerate(height) if idx not in irrelevant_indices]

    # turn coordinates into (left, top, left+width, top+height) format
    actual_boxes = []
    for x, y, w, h in zip(left, top, width, height):
        actual_box = [x, y, x + w, y + h]
        actual_boxes.append(actual_box)

    image_width, image_height = image.size

    # finally, normalize the bounding boxes
    normalized_boxes = []
    for box in actual_boxes:
        normalized_boxes.append(normalize_box(box, image_width, image_height))

    if len(words) != len(normalized_boxes):
        raise ValueError("Not as many words as there are bounding boxes")

    return words, normalized_boxes


class ModelType(ExplicitEnum):
    LayoutLM = "layoutlm"
    LayoutLMv2andv3 = "layoutlmv2andv3"
    VisionEncoderDecoder = "vision_encoder_decoder"


ImageOrName = Union["Image.Image", str]
DEFAULT_MAX_ANSWER_LENGTH = 30


@add_end_docstrings(PIPELINE_INIT_ARGS)
class DocumentQuestionAnsweringPipeline(ChunkPipeline):
    # TODO: Update task_summary docs to include an example with document QA and then update the first sentence
    """
    Document Question Answering pipeline using any `AutoModelForDocumentQuestionAnswering`. The inputs/outputs are
    similar to the (extractive) question answering pipeline; however, the pipeline takes an image (and optional OCR'd
    words/boxes) as input instead of text context.

    This document question answering pipeline can currently be loaded from [`pipeline`] using the following task
    identifier: `"document-question-answering"`.

    The models that this pipeline can use are models that have been fine-tuned on a document question answering task.
    See the up-to-date list of available models on
    [huggingface.co/models](https://huggingface.co/models?filter=document-question-answering).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.model.config.__class__.__name__ == "VisionEncoderDecoderConfig":
            self.model_type = ModelType.VisionEncoderDecoder
            if self.model.config.encoder.model_type != "donut-swin":
                raise ValueError("Currently, the only supported VisionEncoderDecoder model is Donut")
        else:
            # self.check_model_type(MODEL_FOR_DOCUMENT_QUESTION_ANSWERING_MAPPING)
            if self.model.config.__class__.__name__ == "LayoutLMConfig":
                self.model_type = ModelType.LayoutLM
            else:
                self.model_type = ModelType.LayoutLMv2andv3

    def _sanitize_parameters(
        self,
        padding=None,
        doc_stride=None,
        max_question_len=None,
        lang: Optional[str] = None,
        tesseract_config: Optional[str] = None,
        max_answer_len=None,
        max_seq_len=None,
        top_k=None,
        handle_impossible_answer=None,
        **kwargs,
    ):
        preprocess_params, postprocess_params = {}, {}
        if padding is not None:
            preprocess_params["padding"] = padding
        if doc_stride is not None:
            preprocess_params["doc_stride"] = doc_stride
        if max_question_len is not None:
            preprocess_params["max_question_len"] = max_question_len
        if max_seq_len is not None:
            preprocess_params["max_seq_len"] = max_seq_len
        if lang is not None:
            preprocess_params["lang"] = lang
        if tesseract_config is not None:
            preprocess_params["tesseract_config"] = tesseract_config

        if top_k is not None:
            if top_k < 1:
                raise ValueError(f"top_k parameter should be >= 1 (got {top_k})")
            postprocess_params["top_k"] = top_k
        if max_answer_len is not None:
            if max_answer_len < 1:
                raise ValueError(f"max_answer_len parameter should be >= 1 (got {max_answer_len}")
            postprocess_params["max_answer_len"] = max_answer_len
        if handle_impossible_answer is not None:
            postprocess_params["handle_impossible_answer"] = handle_impossible_answer

        return preprocess_params, {}, postprocess_params

    def __call__(
        self,
        image: Union[ImageOrName, List[ImageOrName], List[Tuple]],
        question: Optional[str] = None,
        **kwargs,
    ):
        """
        Answer the question(s) given as inputs by using the document(s). A document is defined as an image and an
        optional list of (word, box) tuples which represent the text in the document. If the `word_boxes` are not
        provided, it will use the Tesseract OCR engine (if available) to extract the words and boxes automatically for
        LayoutLM-like models which require them as input. For Donut, no OCR is run.

        You can invoke the pipeline several ways:

        - `pipeline(image=image, question=question)`
        - `pipeline(image=image, question=question, word_boxes=word_boxes)`
        - `pipeline([{"image": image, "question": question}])`
        - `pipeline([{"image": image, "question": question, "word_boxes": word_boxes}])`

        Args:
            image (`str` or `PIL.Image`):
                The pipeline handles three types of images:

                - A string containing a http link pointing to an image
                - A string containing a local path to an image
                - An image loaded in PIL directly

                The pipeline accepts either a single image or a batch of images. If given a single image, it can be
                broadcasted to multiple questions.
            question (`str`):
                A question to ask of the document.
            word_boxes (`List[str, Tuple[float, float, float, float]]`, *optional*):
                A list of words and bounding boxes (normalized 0->1000). If you provide this optional input, then the
                pipeline will use these words and boxes instead of running OCR on the image to derive them for models
                that need them (e.g. LayoutLM). This allows you to reuse OCR'd results across many invocations of the
                pipeline without having to re-run it each time.
            top_k (`int`, *optional*, defaults to 1):
                The number of answers to return (will be chosen by order of likelihood). Note that we return less than
                top_k answers if there are not enough options available within the context.
            doc_stride (`int`, *optional*, defaults to 128):
                If the words in the document are too long to fit with the question for the model, it will be split in
                several chunks with some overlap. This argument controls the size of that overlap.
            max_answer_len (`int`, *optional*, defaults to 15):
                The maximum length of predicted answers (e.g., only answers with a shorter length are considered).
            max_seq_len (`int`, *optional*, defaults to 384):
                The maximum length of the total sentence (context + question) in tokens of each chunk passed to the
                model. The context will be split in several chunks (using `doc_stride` as overlap) if needed.
            max_question_len (`int`, *optional*, defaults to 64):
                The maximum length of the question after tokenization. It will be truncated if needed.
            handle_impossible_answer (`bool`, *optional*, defaults to `False`):
                Whether or not we accept impossible as an answer.
            lang (`str`, *optional*):
                Language to use while running OCR. Defaults to english.
            tesseract_config (`str`, *optional*):
                Additional flags to pass to tesseract while running OCR.

        Return:
            A `dict` or a list of `dict`: Each result comes as a dictionary with the following keys:

            - **score** (`float`) -- The probability associated to the answer.
            - **start** (`int`) -- The start word index of the answer (in the OCR'd version of the input or provided
              `word_boxes`).
            - **end** (`int`) -- The end word index of the answer (in the OCR'd version of the input or provided
              `word_boxes`).
            - **answer** (`str`) -- The answer to the question.
            - **words** (`list[int]`) -- The index of each word/box pair that is in the answer
            - **page** (`int`) -- The page of the answer
        """
        if question is None:
            question = image["question"]
            image = image["image"]

        if isinstance(image, list):
            normalized_images = (i if isinstance(i, (tuple, list)) else (i, None) for i in image)
        else:
            normalized_images = [(image, None)]

        return super().__call__({"question": question, "pages": normalized_images}, **kwargs)

    def preprocess(
        self,
        input,
        padding="do_not_pad",
        doc_stride=None,
        max_question_len=64,
        max_seq_len=None,
        word_boxes: Tuple[str, List[float]] = None,
        lang=None,
        tesseract_config="",
    ):
        # NOTE: This code mirrors the code in question answering and will be implemented in a follow up PR
        # to support documents with enough tokens that overflow the model's window
        if max_seq_len is None:
            max_seq_len = min(self.tokenizer.model_max_length, 512)

        if doc_stride is None:
            doc_stride = min(max_seq_len // 2, 256)

        for page_idx, (image, word_boxes) in enumerate(input["pages"]):
            image_features = {}
            if image is not None:
                image = load_image(image)
                if self.feature_extractor is not None:
                    image_features.update(self.feature_extractor(images=image, return_tensors=self.framework))
                elif self.model_type == ModelType.VisionEncoderDecoder:
                    raise ValueError(
                        "If you are using a VisionEncoderDecoderModel, you must provide a feature extractor"
                    )

            words, boxes = None, None
            if not self.model_type == ModelType.VisionEncoderDecoder:
                if word_boxes is not None:
                    words = [x[0] for x in word_boxes]
                    boxes = [x[1] for x in word_boxes]
                elif "words" in image_features and "boxes" in image_features:
                    words = image_features.pop("words")[0]
                    boxes = image_features.pop("boxes")[0]
                elif image is not None:
                    if not TESSERACT_LOADED:
                        raise ValueError(
                            "If you provide an image without word_boxes, then the pipeline will run OCR using"
                            " Tesseract, but pytesseract is not available. Install it with pip install pytesseract."
                        )
                    if TESSERACT_LOADED:
                        words, boxes = apply_tesseract(image, lang=lang, tesseract_config=tesseract_config)
                else:
                    raise ValueError(
                        "You must provide an image or word_boxes. If you provide an image, the pipeline will"
                        " automatically run OCR to derive words and boxes"
                    )

            if self.tokenizer.padding_side != "right":
                raise ValueError(
                    "Document question answering only supports tokenizers whose padding side is 'right', not"
                    f" {self.tokenizer.padding_side}"
                )

            if self.model_type == ModelType.VisionEncoderDecoder:
                task_prompt = f'<s_docvqa><s_question>{input["question"]}</s_question><s_answer>'
                # Adapted from https://huggingface.co/spaces/nielsr/donut-docvqa/blob/main/app.py
                encoding = {
                    "inputs": image_features["pixel_values"],
                    "decoder_input_ids": self.tokenizer(
                        task_prompt, add_special_tokens=False, return_tensors=self.framework
                    ).input_ids,
                    "return_dict_in_generate": True,
                }

                yield {
                    **encoding,
                    "p_mask": None,
                    "word_ids": None,
                    "words": None,
                    "page": None,
                    "output_attentions": True,
                }
            else:
                tokenizer_kwargs = {}
                if self.model_type == ModelType.LayoutLM:
                    tokenizer_kwargs["text"] = input["question"].split()
                    tokenizer_kwargs["text_pair"] = words
                    tokenizer_kwargs["is_split_into_words"] = True
                else:
                    tokenizer_kwargs["text"] = [input["question"]]
                    tokenizer_kwargs["text_pair"] = [words]
                    tokenizer_kwargs["boxes"] = [boxes]

                encoding = self.tokenizer(
                    padding=padding,
                    max_length=max_seq_len,
                    stride=doc_stride,
                    truncation="only_second",
                    return_overflowing_tokens=True,
                    **tokenizer_kwargs,
                )

                if "pixel_values" in image_features:
                    encoding["image"] = image_features.pop("pixel_values")

                num_spans = len(encoding["input_ids"])

                # p_mask: mask with 1 for token than cannot be in the answer (0 for token which can be in an answer)
                # We put 0 on the tokens from the context and 1 everywhere else (question and special tokens)
                # This logic mirrors the logic in the question_answering pipeline
                p_mask = [[tok != 1 for tok in encoding.sequence_ids(span_id)] for span_id in range(num_spans)]

                for span_idx in range(num_spans):
                    if self.framework == "pt":
                        span_encoding = {k: torch.tensor(v[span_idx : span_idx + 1]) for (k, v) in encoding.items()}
                        span_encoding.update(
                            {k: v for (k, v) in image_features.items()}
                        )  # TODO: Verify cardinality is correct
                    else:
                        raise ValueError("Unsupported: Tensorflow preprocessing for DocumentQuestionAnsweringPipeline")

                    input_ids_span_idx = encoding["input_ids"][span_idx]
                    # keep the cls_token unmasked (some models use it to indicate unanswerable questions)
                    if self.tokenizer.cls_token_id is not None:
                        cls_indices = np.nonzero(np.array(input_ids_span_idx) == self.tokenizer.cls_token_id)[0]
                        for cls_index in cls_indices:
                            p_mask[span_idx][cls_index] = 0

                    # For each span, place a bounding box [0,0,0,0] for question and CLS tokens, [1000,1000,1000,1000]
                    # for SEP tokens, and the word's bounding box for words in the original document.
                    if "boxes" not in tokenizer_kwargs:
                        bbox = []

                        for input_id, sequence_id, word_id in zip(
                            encoding.input_ids[span_idx],
                            encoding.sequence_ids(span_idx),
                            encoding.word_ids(span_idx),
                        ):
                            if sequence_id == 1:
                                bbox.append(boxes[word_id])
                            elif input_id == self.tokenizer.sep_token_id:
                                bbox.append([1000] * 4)
                            else:
                                bbox.append([0] * 4)

                        if self.framework == "pt":
                            span_encoding["bbox"] = torch.tensor(bbox).unsqueeze(0)
                        elif self.framework == "tf":
                            raise ValueError(
                                "Unsupported: Tensorflow preprocessing for DocumentQuestionAnsweringPipeline"
                            )

                    yield {
                        **span_encoding,
                        "p_mask": p_mask[span_idx],
                        "word_ids": encoding.word_ids(span_idx),
                        "words": words,
                        "page": page_idx,
                    }

    def _forward(self, model_inputs):
        p_mask = model_inputs.pop("p_mask", None)
        word_ids = model_inputs.pop("word_ids", None)
        words = model_inputs.pop("words", None)
        page = model_inputs.pop("page", None)

        if "overflow_to_sample_mapping" in model_inputs:
            model_inputs.pop("overflow_to_sample_mapping")

        if self.model_type == ModelType.VisionEncoderDecoder:
            model_outputs = self.model.generate(**model_inputs)
        else:
            model_outputs = self.model(**model_inputs)

        model_outputs["p_mask"] = p_mask
        model_outputs["word_ids"] = word_ids
        model_outputs["words"] = words
        model_outputs["page"] = page
        model_outputs["attention_mask"] = model_inputs.get("attention_mask", None)
        return model_outputs

    def postprocess(self, model_outputs, top_k=1, **kwargs):
        if self.model_type == ModelType.VisionEncoderDecoder:
            answers = [self.postprocess_encoder_decoder_single(o) for o in model_outputs]
        else:
            answers = self.postprocess_extractive_qa(model_outputs, top_k=top_k, **kwargs)

        answers = sorted(answers, key=lambda x: x.get("score", 0), reverse=True)[:top_k]
        return answers

    def postprocess_encoder_decoder_single(self, model_outputs, **kwargs):
        sequence = self.tokenizer.batch_decode(model_outputs.sequences)[0]

        # TODO: A lot of this logic is specific to Donut and should probably be handled in the tokenizer
        # (see https://github.com/huggingface/transformers/pull/18414/files#r961747408 for more context).
        sequence = sequence.replace(self.tokenizer.eos_token, "").replace(self.tokenizer.pad_token, "")
        sequence = re.sub(r"<.*?>", "", sequence, count=1).strip()  # remove first task start token
        ret = {
            "answer": None,
        }

        answer = re.search(r"<s_answer>(.*)</s_answer>", sequence)
        if answer is not None:
            ret["answer"] = answer.group(1).strip()
        return ret

    def postprocess_extractive_qa(
        self, model_outputs, top_k=1, handle_impossible_answer=False, max_answer_len=None, **kwargs
    ):
        min_null_score = 1000000  # large and positive
        answers = []

        if max_answer_len is None:
            if hasattr(self.model.config, "token_classification") and self.model.config.token_classification:
                # If this model has token classification, then use a much longer max answer length and
                # let the classifier remove things
                max_answer_len = self.tokenizer.model_max_length
            else:
                max_answer_len = DEFAULT_MAX_ANSWER_LENGTH

        for output in model_outputs:
            words = output["words"]

            starts, ends, scores, min_null_score = select_starts_ends(
                output["start_logits"],
                output["end_logits"],
                output["p_mask"],
                output["attention_mask"].numpy() if output.get("attention_mask", None) is not None else None,
                min_null_score,
                top_k,
                handle_impossible_answer,
                max_answer_len,
            )
            word_ids = output["word_ids"]
            for start, end, score in zip(starts, ends, scores):
                if "token_logits" in output:
                    predicted_token_classes = (
                        output["token_logits"][
                            0,
                            start : end + 1,
                        ]
                        .argmax(axis=1)
                        .cpu()
                        .numpy()
                    )
                    assert np.setdiff1d(predicted_token_classes, [0, 1]).shape == (0,)
                    token_indices = np.flatnonzero(predicted_token_classes) + start
                else:
                    token_indices = range(start, end + 1)

                answer_word_ids = list(unique_everseen([word_ids[i] for i in token_indices]))
                if len(answer_word_ids) > 0 and answer_word_ids[0] is not None and answer_word_ids[-1] is not None:
                    answers.append(
                        {
                            "score": float(score),
                            "answer": " ".join(words[i] for i in answer_word_ids),
                            "word_ids": answer_word_ids,
                            "page": output["page"],
                        }
                    )

        if handle_impossible_answer:
            answers.append({"score": min_null_score, "answer": "", "start": 0, "end": 0})

        return answers

"""
Utility classes and functions
"""
import math
import matplotlib.pyplot as plt
import numpy as np
import os
import re
import string
import time
import torch
import torch.nn.functional as F
import torch.utils.data as data
import tqdm
import ujson as json

from collections import Counter
from IPython.display import clear_output
from itertools import repeat
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModel


class SquadDataset(torch.utils.data.Dataset):

    """

    Create dataset object
    Credits: Fine-Tuning With SQuAD 2.0
    https://gist.github.com/jamescalam/55daf50c8da9eb3a7c18de058bc139a3
    with minor changes

    """

    def __init__(self, encodings):
        self.encodings = encodings

    def __getitem__(self, idx):
        return {key: val[idx] for key, val in self.encodings.items()}

    def __len__(self):
        return len(self.encodings.input_ids)

class AverageMeter:

    """

    Keep track of average values over time.
    Credits: Starter code for Stanford CS224n default final project on SQuAD 2.0
    https://github.com/chrischute/squad

    """

    def __init__(self):
        self.avg = 0


        self.sum = 0
        self.count = 0

    def reset(self):
        """Reset meter."""
        self.__init__()

    def update(self, val, num_samples=1):
        """Update meter with new value `val`, the average of `num` samples.

        Args:
            val (float): Average value to update the meter with.
            num_samples (int): Number of samples that were averaged to
                produce `val`.
        """
        self.count += num_samples
        self.sum += val * num_samples
        self.avg = self.sum / self.count


def visualize(tbx, preds, answers, contexts, questions, question_context, step, split, num_visuals):

    """

    Visualize text examples to TensorBoard.

    Credits: Starter code for Stanford CS224n default final project on SQuAD 2.0
    https://github.com/chrischute/squad
    with some changes
    Args:
        tbx (tensorboardX.SummaryWriter): Summary writer.
        preds (dict): dict of predicted answers in format question id: text.
        answers (dict): dictionary with answer examples in format
        {question id: {text: answer text,
                       answer_start: index number of character in context where answer starts,
                        context_id: native context id}}
        contexts (dict): dictionary with context examples in format {question id: context}
        questions: (dict): dictionary with question examples in format {question id: question}
        question_context (dict): dictionary with native contexts to each question in format
        {question id: context id}
        step (int): Number of examples seen so far during training.
        split (str): Name of data split being visualized.
        num_visuals (int): Number of visuals to select at random from preds.

    """

    if num_visuals <= 0:
        return
    if num_visuals > len(preds):
        num_visuals = len(preds)

    visual_ids = np.random.choice(list(preds), size=num_visuals, replace=False)

    for i, id_ in enumerate(visual_ids):
        # pred = pred_dict[id_] or 'N/A'
        # question = questions[id_]
        # context = contexts[id_]
        # answer = answers[id_]['text']
        pred = preds[id_]
        question = questions[id_]
        context_id = question_context[id_]
        context = contexts[context_id]
        answer = answers[id_]['text']

        tbl_fmt = (f'- **Question:** {question}\n'
                   + f'- **Context:** {context}\n'
                   + f'- **Answer:** {answer}\n'
                   + f'- **Prediction:** {pred}')
        tbx.add_text(tag=f'{split}/{i+1}_of_{num_visuals}',
                     text_string=tbl_fmt,
                     global_step=step)


def discretize(p_start, p_end, max_len=15, no_answer=False):

    """

    Discretize soft predictions to get start and end indices.

    Credits: Starter code for Stanford CS224n default final project on SQuAD 2.0
    https://github.com/chrischute/squad

    Choose the pair `(i, j)` of indices that maximizes `p1[i] * p2[j]`
    subject to `i <= j` and `j - i + 1 <= max_len`.

    Args:
        p_start (torch.Tensor): Soft predictions for start index.
            Shape (batch_size, context_len).
        p_end (torch.Tensor): Soft predictions for end index.
            Shape (batch_size, context_len).
        max_len (int): Maximum length of the discretized prediction.
            I.e., enforce that `preds[i, 1] - preds[i, 0] + 1 <= max_len`.
        no_answer (bool): Treat 0-index as the no-answer prediction. Consider
            a prediction no-answer if `preds[0, 0] * preds[0, 1]` is greater
            than the probability assigned to the max-probability span.

    Returns:
        start_idxs (torch.Tensor): Hard predictions for start index.
            Shape (batch_size,)
        end_idxs (torch.Tensor): Hard predictions for end index.
            Shape (batch_size,)
        p - max of joint probability
            Shape (batch_size,)
        
    """

    if p_start.min() < 0 or p_start.max() > 1 \
            or p_end.min() < 0 or p_end.max() > 1:
        raise ValueError('Expected p_start and p_end to have values in [0, 1]')

    # Compute pairwise probabilities
    p_start = p_start.unsqueeze(dim=2)
    p_end = p_end.unsqueeze(dim=1)
    p_joint = torch.matmul(p_start, p_end)  # (batch_size, c_len, c_len)

    # Restrict to pairs (i, j) such that i <= j <= i + max_len - 1
    c_len, device = p_start.size(1), p_start.device
    is_legal_pair = torch.triu(torch.ones((c_len, c_len), device=device))
    is_legal_pair -= torch.triu(torch.ones((c_len, c_len), device=device),
                                diagonal=max_len)
    if no_answer:
        # Index 0 is no-answer
        p_no_answer = p_joint[:, 0, 0].clone()
        is_legal_pair[0, :] = 0
        is_legal_pair[:, 0] = 0
    else:
        p_no_answer = None
    p_joint *= is_legal_pair

    # Take pair (i, j) that maximizes p_joint
    max_in_row, _ = torch.max(p_joint, dim=2)
    #print(max_in_row)
    max_in_col, _ = torch.max(p_joint, dim=1)
    #print(max_in_col)
    start_idxs = torch.argmax(max_in_row, dim=-1)
    end_idxs = torch.argmax(max_in_col, dim=-1)
    p_joint_max = torch.amax(p_joint, dim=(1,2)).cpu().numpy()

    if no_answer:
        # Predict no-answer whenever p_no_answer > max_prob
        max_prob, _ = torch.max(max_in_col, dim=-1)
        start_idxs[p_no_answer > max_prob] = 0
        end_idxs[p_no_answer > max_prob] = 0

    start_idxs = start_idxs.detach().cpu().numpy()
    end_idxs = end_idxs.detach().cpu().numpy()
    return start_idxs, end_idxs, p_joint_max


def metric_max_over_ground_truths(metric_fn, prediction, ground_truth):

    """

    Credits: Starter code for Stanford CS224n default final project on SQuAD 2.0
    https://github.com/chrischute/squad
    with some changes

    """

    if not ground_truth:
        return metric_fn(prediction, '')
    return metric_fn(prediction, ground_truth)


def eval_dicts(answers, pred_dict, no_answer):

    """

    Credits: Starter code for Stanford CS224n default final project on SQuAD 2.0
    https://github.com/chrischute/squad
    with minor changes

    """

    avna = f1 = em = total = 0
    for key, value in pred_dict.items():
        total += 1
        ground_truths = answers[key]['text']
        prediction = value
        em += metric_max_over_ground_truths(compute_em, prediction, ground_truths)
        f1 += metric_max_over_ground_truths(compute_f1, prediction, ground_truths)
        if no_answer:
            avna += compute_avna(prediction, ground_truths)

    eval_dict = {'EM': 100. * em / total,
                 'F1': 100. * f1 / total}

    if no_answer:
        eval_dict['AvNA'] = 100. * avna / total
    
    result = {'F1': eval_dict['F1'], 'EM': eval_dict['EM']}

    return result


def read_squad(path):

    """

    Credits: Fine-Tuning With SQuAD 2.0
    https://gist.github.com/jamescalam/55daf50c8da9eb3a7c18de058bc139a3
    with some changes

    Read file with SberQuad data and construct dictionaries in appropriate format

    Args:
        path (string): path to file with data.

    Returns:
        contexts_dict (dict): dictionary with context examples in format {context id: context}
        questions_dict (dict): dictionary with question examples in format {question id: question}
        answers_dict (dict): dictionary with answer examples in format
        {question id: {text: answer text,
                       answer_start: index number of character in context where answer starts,
                        context_id: native context id}}
        question_context_dict (dict): dictionary with native contexts to each question in format 
        {question id: context id}

    """

    with open(path, 'rb') as f:
        squad_dict = json.load(f)

    contexts_dict = {}
    answers_dict = {}
    questions_dict = {}
    question_context_dict = {}

    # iterate through all data in squad data
    for group in squad_dict['data']:
        for passage in group['paragraphs']:
            context = passage['context']
            context_id = int(passage['id'])
            contexts_dict[context_id] = context
            for qa in passage['qas']:
                question = qa['question']
                question_id = int(qa['id'])
                if 'plausible_answers' in qa.keys():
                    access = 'plausible_answers'
                else:
                    access = 'answers'
                for answer in qa['answers']:
                    # if answer starts with a wight space, remove it
                    # and correct field 'answer_start'
                    if answer['text'][0] == " ":
                        cut_string = answer['text'].lstrip()
                        cut_characters_number = len(answer['text']) - len(cut_string)
                        answer['text'] = cut_string
                        answer['answer_start'] += cut_characters_number
                    # append data to dicts
                    answer['context_id'] = context_id
                    questions_dict[question_id] = question
                    answers_dict[question_id] = answer
                    question_context_dict[question_id] = context_id

    return contexts_dict, questions_dict, answers_dict, question_context_dict


def read_lb(path):

    """

    Read file with LB data and construct dictionaries in appropriate format

    Args:
        path (string): path to file with data.

    Returns:
        contexts_dict (dict): dictionary with context examples in format {context id: context}
        questions_dict (dict): dictionary with question examples in format {question id: question}
        answers_dict (dict): dictionary with answer examples in format
        {question id: {text: answer text,
                       answer_start: index number of character in context where answer starts,
                        context_id: native context id}}
        question_context_dict (dict): dictionary with native contexts to each question in format
        {question id: context id}

    """

    with open(path, 'rb') as f:
        squad_dict = json.load(f)

    contexts_dict = {}
    answers_dict = {}
    questions_dict = {}
    question_context_dict = {}

    for line in squad_dict['data']:
        answer = {}
        context = line['text']
        context_id = int(line['id'])
        contexts_dict[context_id] = context
        # print(context_id)
        if bool(line['question']):
            question = line['question']
            question_id = int(line['question_id'])
            answer['text'] = str(line['answer'])
            answer['answer_start'] = line['answer_start']
            answer['context_id'] = context_id
            if str(line['answer'])[0] == " ":
                cut_string = line['answer'].lstrip()
                cut_characters_number = len(line['answer']) - len(cut_string)
                answer['text'] = cut_string
                answer['answer_start'] += cut_characters_number
            questions_dict[question_id] = question
            answers_dict[question_id] = answer
            question_context_dict[question_id] = context_id

    return contexts_dict, questions_dict, answers_dict, question_context_dict


def read_context_lb(path):
    with open(path, 'rb') as f:
        squad_dict = json.load(f)
    contexts_dict = {}
    for line in squad_dict['data']:
        context = line['text']
        context_id = int(line['id'])
        contexts_dict[context_id] = context

    return contexts_dict


def read_question_lb(path):
    with open(path, 'rb') as f:
        squad_dict = json.load(f)

    answers_dict = {}
    questions_dict = {}
    question_context_dict = {}

    for line in squad_dict['data']:
        question = line['question']
        question_id = int(line['question_id'])
        questions_dict[question_id] = question
        answer = {}
        try:
            context_id = int(line['context_id'])
            answer['text'] = str(line['answer'])
            answer['context_id'] = context_id
            if str(line['answer'])[0] == " ":
                answer['text'] = line['answer'].lstrip()

            answers_dict[question_id] = answer
            question_context_dict[question_id] = context_id
        except KeyError:
            answer['text'] = None
            answer['context_id'] = None
            answers_dict[question_id] = answer
            question_context_dict[question_id] = None

    return questions_dict, answers_dict, question_context_dict


def add_end_idx(answers, contexts):

    """

    Credits: Fine-Tuning With SQuAD 2.0
    https://gist.github.com/jamescalam/55daf50c8da9eb3a7c18de058bc139a3
    with minor changes

    Add field 'answer_end' to dict with answer examples.
    After function applying answers has format
                in format {question id:
                        {text: answer text,
                        answer_start: index number of character in context where answer starts,
                        answer_end: index number of character in context where answer ends}}

    Args:
        answers (dict): dictionary with answer examples in format
        {question id: {text: answer text,
                       answer_start: index number of character in context where answer starts,
                        context_id: native context id}}
        contexts (dict): dictionary with context examples in format {question id: context}

    """

    # loop through each answer-context pair
    # for answer, context in zip(answers, contexts):
    for key, answer in answers.items():
        # gold_text refers to the answer we are expecting to find in context
        context_id = answer['context_id']
        context = contexts[context_id]
        gold_text = answer['text']
        # we already know the start index
        start_idx = answer['answer_start']
        # and ideally this would be the end index...
        end_idx = start_idx + len(gold_text)

        # ...however, sometimes squad answers are off by a character or two
        if context[start_idx:end_idx] == gold_text:
            # if the answer is not off :)
            answer['answer_end'] = end_idx
        else:
            for n in [1, 2]:
                if context[start_idx-n:end_idx-n] == gold_text:
                    # this means the answer is off by 'n' tokens
                    answer['answer_start'] = start_idx - n
                    answer['answer_end'] = end_idx - n


def add_token_positions(encodings, answers, max_length):

    """

    Credits: Fine-Tuning With SQuAD 2.0
    https://gist.github.com/jamescalam/55daf50c8da9eb3a7c18de058bc139a3
    with some changes

    Add start and end token positions to encodings
    and get example ids where answer was truncated from its context
    After function applying encodings get 3 additional lists:
        'start_positions': list of index token numbers where answers start
        'end_positions': list of index token numbers where answers end
        'ids': list of question ids

    Args:
        encodings (BatchEncoding): encodings
        answers_dict (dict): dictionary with answer examples in format
        {question id: {text: answer text,
                       answer_start: index number of character in context where answer starts,
                       context_id: native context id,
                       answer_end: index number of character in context where answer ends}}
        max_length (int): maximum lenght of encoding sequence

    Returns:
        truncated_idxs (list): list of examples ids, where context doesn't contain answer
            because context was truncated

    """

    # initialize lists to contain the token indices of answer start/end
    start_positions = []
    end_positions = []
    truncated_idxs = []
    i = 0
    for key, answer in answers.items():
        # append start/end token position using char_to_token method
        start_positions.append(encodings.char_to_token(i, answer['answer_start']))
        end_positions.append(encodings.char_to_token(i, answer['answer_end']))

        # if start position is None, the answer passage has been truncated
        if start_positions[-1] is None:
            truncated_idxs.append(key)
            start_positions[-1] = max_length - 1
        # end position cannot be found, char_to_token found space, so shift one token forward
        go_back = 1
        while end_positions[-1] is None:
            end_positions[-1] = encodings.char_to_token(i, answer['answer_end'] - go_back)
            go_back +=1
        i += 1
    # update our encodings object with the new token-based start/end positions
    encodings.update({'start_positions': start_positions,
                      'end_positions': end_positions,
                      'ids': list(answers.keys())})
    return truncated_idxs


def train(model, iterator, optimizer, criterion, train_history=None, valid_history=None):

    """

    Train model

    Args:
        model (models): model to be evaluated
        iterator (DataLoader): Dataloader with encodings
        optimizer (torch.optim): optimizer
        criterion (NLLLoss): loss function
        train_history (list) - list of train loss values over epoches to plot graphs
        valid_history (list) - list of valid loss values over epoches to plot graphs

    Returns:
        epoch_loss / len(iterator): loss value

    """

    model.train()
    epoch_loss = 0
    history = []
    for i, batch in tqdm.tqdm(enumerate(iterator)):
        optimizer.zero_grad()
        input_ids = batch['input_ids'].cuda()
        logp_start, logp_end = model(input_ids)
        target_start = batch['start_positions'].cuda()
        target_end = batch['end_positions'].cuda()
        loss = criterion(logp_start, target_start) + criterion(logp_end, target_end)
        loss.backward()
        optimizer.step()
        # Let's clip the gradient
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
        epoch_loss += loss.item()
        history.append(loss.cpu().data.numpy())
        if (i + 1) % 10 == 0:
            fig, ax = plt.subplots(nrows=1, ncols=2, figsize=(12, 8))

            clear_output(True)
            ax[0].plot(history, label='train loss')
            ax[0].set_xlabel('Batch')
            ax[0].set_title('Train loss')
            if train_history is not None:
                ax[1].plot(train_history, label='general train history')
                ax[1].set_xlabel('Epoch')
            if valid_history is not None:
                ax[1].plot(valid_history, label='general valid history')
            plt.legend()

            plt.show()
    return epoch_loss / len(iterator)


def get_logits(model, iterator, get_loss=False, criterion=None):

    """

    Compute predictions (logits) during evaluation stage

    Args:
        model (models): model to be evaluated
        iterator (DataLoader): Dataloader with encodings
        get_loss (bool):
            False: don't compute loss between predictions and true values
            True: compute loss between predictions and true values
        criterion (NLLLoss): loss function

    Returns:
        logp_start (torch.Tensor): predictions (logits) for start index.
                Shape [iterator lenght, context_len].
        logp_end (torch.Tensor): predictions (logits) for end index.
                Shape [iterator lenght, context_len].
        loss (float): loss value:
            If get_loss = False:
                0
            If get_loss = True:
                average loss value for all batches

    """

    model.eval()
    logp_start = torch.empty(0, dtype=torch.float32)
    logp_end = torch.empty(0, dtype=torch.float32)
    with torch.no_grad():
        if get_loss:
            # create an instance to measure loss
            nll_meter = AverageMeter()
        for batch in tqdm.tqdm(iterator):
            input_ids = batch['input_ids'].cuda()
            # get predictions for a batch
            logp_start_batch, logp_end_batch = model(input_ids)
            # add them to tensor with all predictions for iterator
            logp_start = torch.cat((logp_start, logp_start_batch.cpu()), 0)
            logp_end = torch.cat((logp_end, logp_end_batch.cpu()), 0)
            if get_loss:
                # compute loss for a batch and update nll_meter
                target_start = batch['start_positions'].cuda()
                target_end = batch['end_positions'].cuda()
                loss = criterion(logp_start_batch, target_start) + criterion(logp_end_batch, target_end)
                nll_meter.update(loss.item(), input_ids.shape[0])
        if get_loss:
            loss = nll_meter.avg
        else:
            loss = 0

    return logp_start, logp_end, loss


def get_answer(logp_start, logp_end, doc, doc_id, query_id, encoding, weights=None):
    """

        Get predicted text answer to a query based on reader model predictions

        Args:
            logp_start (torch.Tensor): predictions (logits) for start index.
                Shape [number of retrieved documents for all queries, context_len].
            logp_end (torch.Tensor): predictions (logits) for end index.
                Shape [number of retrieved documents for all queries, context_len].
            doc (list): Input strings. Lenght [number of retrieved documents for all queries]
            doc_id (list): document ids. Lenght [number of retrieved documents for all queries]
            query_id (list): query ids. Lenght [number of queries]
            encoding (BatchEncoding): encodings to reconstruct text answer
            weights (ndarray): coefficients to correct probability of correct answer given by reader model.
            Shape [number of queries, number of retrieved documents for each query]

        Returns:
            pred_answers (dict): a dict with text answers
            in format {query_id: answer text}. Lenght [number of queries]
            selected_doc_id (list): a list with finally selected document ids for each query.
            Lenght [number of queries]

        """

    p_start, p_end = logp_start.exp(), logp_end.exp()
    # get start/end indices based on predictions
    # and joint probability that start and end predictions are correct
    pred_token_start, pred_token_end, p_joint = discretize(p_start, p_end, 100, False)
    pred_answers = {}

    # get number of docs per query
    n = len(doc) // len(query_id)
    if n > 1:
        # if there are more than one docs per query, we deal with open domain QA.
        # it means that for each query we compute start/end token probability for each doc
        # and than for each query we select doc index with max joint probability p_joint.
        # if weights are given, we need to correct model probability with them

        # if weights aren't given consider they equal to 1
        if type(weights) != np.ndarray:
            weights = np.ones((len(query_id), n))
        # p_joint has shape [total number of docs].
        # Transform it in more convenient way: [number of queries, n].
        # Each row in this matrix matches a query.
        # Each value in a row is a model confidence to predict correct start/end of the answer
        # for each of n retrieved documents (in columns)
        model_answer_probability = np.reshape(p_joint, (-1, n))
        # correct model probability with given weights
        final_answer_probability = model_answer_probability * weights
        # For each row select index of the document with the highest probability.
        # Shape [number of queries, n]
        selected_doc_index_by_query = np.argmax(final_answer_probability, axis=1)
        # transform selected doc_id indices in the matrix to
        # indices in original list doc_id
        shift_by_query = np.arange(len(query_id), dtype=np.int32) * n
        selected_doc_index = selected_doc_index_by_query + shift_by_query
        # finally get doc_id from doc_id index
        selected_doc_id = np.array(doc_id)[selected_doc_index]
    else:
        # If n = 1, we don't need to select the most probable document for a query,
        # because there is just one of them for each query
        # So generate selected_doc_index as list [0, 1, ...] of lenght [number of docs]
        # and selected doc_id is equal to input doc_id
        selected_doc_index = [i for i in range(len(doc_id))]
        selected_doc_id = doc_id
    # look for a text answer for each question based on selected doc_id and predicted start/end of the answer
    for question_id, doc_index in zip(query_id, selected_doc_index):
        char_answer_start = encoding.token_to_chars(doc_index, pred_token_start[doc_index]).start
        char_answer_end = encoding.token_to_chars(doc_index, pred_token_end[doc_index]).end
        # predicted answer is a substring of a document between start and end characters
        pred_answers[question_id] = doc[doc_index][char_answer_start:char_answer_end]
    return pred_answers, selected_doc_id


def retriever_accuracy(similarity, query_id, doc_id, question_context, n):

    """

    Compute accuracy for retriever model

    Args:
        similarity (ndarray): matrix  of similarity measure between queries and documents.
        Shape [number of documents, number of queries]
        query_id (list): query ids. Lenght [number of queries]
        doc_id (list): document ids. Lenght [number of documents]
        question_context (dict): a dict with native document id for each query id.
        Format {query_id: doc_id}. Lenght [number of queries]
        n: number of retrieved documents for each query

    Returns:
        ratio of queries with correctly found document ids to total number of queries

    """

    # return document indexes with top-n similatiry measure
    retrieved_doc_index = np.argpartition(similarity, -n, axis=0)[-n:].T
    # compute number of errors: if native document id isn't in retrieved document_id list
    err = 0
    for question_id, doc_index in zip(query_id, retrieved_doc_index):
        native_doc_id = question_context[question_id]
        retrieved_doc_id = np.array(doc_id)[doc_index] # tranform doc indexes into ids
        if native_doc_id not in retrieved_doc_id:
            err +=1
    return 1 - err / retrieved_doc_index.shape[0]
    

def epoch_time(start_time, end_time):

    """

    Calculate number of minutes and seconds between args start_time and end_time

    Args:
        start_time (float): beginning of time period
        end_time (float): end of time period

    Returns:
        elapsed_mins (int): number of minutes
        elapsed_secs (int): number of seconds

    """

    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))

    return elapsed_mins, elapsed_secs


# All methods below this line are from the official SQuAD 2.0 eval script
# https://worksheets.codalab.org/rest/bundles/0x6b567e1cf2e041ec80d7098f031c5c9e/contents/blob/
def normalize_answer(s):
    """Convert to lowercase and remove punctuation, articles and extra whitespace."""

    def remove_articles(text):
        regex = re.compile(r'\b(a|an|the)\b', re.UNICODE)
        return re.sub(regex, ' ', text)

    def white_space_fix(text):
        return ' '.join(text.split())

    def remove_punc(text):
        exclude = set(string.punctuation)
        return ''.join(ch for ch in text if ch not in exclude)

    def lower(text):
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def get_tokens(s):
    if not s:
        return []
    return normalize_answer(s).split()


def compute_em(a_gold, a_pred):
    return int(normalize_answer(a_gold) == normalize_answer(a_pred))


def compute_f1(a_gold, a_pred):
    gold_toks = get_tokens(a_gold)
    pred_toks = get_tokens(a_pred)
    common = Counter(gold_toks) & Counter(pred_toks)
    num_same = sum(common.values())
    if len(gold_toks) == 0 or len(pred_toks) == 0:
        # If either is no-answer, then F1 is 1 if they agree, 0 otherwise
        return int(gold_toks == pred_toks)
    if num_same == 0:
        return 0
    precision = 1.0 * num_same / len(pred_toks)
    recall = 1.0 * num_same / len(gold_toks)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1

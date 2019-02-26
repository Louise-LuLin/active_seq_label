# -*- coding: utf-8 -*-
# @Author: Jie Yang
# @Date:   2017-12-04 23:19:38
# @Last Modified by:   Jie Yang,     Contact: jieynlp@gmail.com
# @Last Modified time: 2018-01-15 21:18:16
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.autograd import Variable
START_TAG = -2
STOP_TAG = -1


# Compute log sum exp in a numerically stable way for the forward algorithm
def log_sum_exp(vec, m_size):
    """
    calculate log of exp sum
    args:
        vec (batch_size, vanishing_dim, hidden_dim) : input tensor
        m_size : hidden_dim
    return:
        batch_size, hidden_dim
    """
    _, idx = torch.max(vec, 1)  # B * 1 * M
    max_score = torch.gather(vec, 1, idx.view(-1, 1, m_size)).view(-1, 1, m_size)  # B * M
    return max_score.view(-1, m_size) + torch.log(torch.sum(torch.exp(vec - max_score.expand_as(vec)), 1)).view(-1, m_size)  # B * M

class CRF(nn.Module):

    def __init__(self, tagset_size, gpu):
        super(CRF, self).__init__()
        print "build batched crf..."
        self.gpu = gpu
        # Matrix of transition parameters.  Entry i,j is the score of transitioning *to* i *from* j.
        self.average_batch = False
        self.tagset_size = tagset_size
        # # We add 2 here, because of START_TAG and STOP_TAG
        # # transitions (f_tag_size, t_tag_size), transition value from f_tag to t_tag
        init_transitions = torch.zeros(self.tagset_size+2, self.tagset_size+2)
        init_transitions[:,START_TAG] = -1000.0
        init_transitions[STOP_TAG,:] = -1000.0
        # init_transitions[:,0] = -1000.0
        # init_transitions[0,:] = -1000.0
        if self.gpu:
            init_transitions = init_transitions.cuda()
        self.transitions = nn.Parameter(init_transitions)

        # self.transitions = nn.Parameter(torch.Tensor(self.tagset_size+2, self.tagset_size+2))
        # self.transitions.data.zero_()

    def _calculate_PZ(self, feats, mask):
        """
            input:
                feats: (batch, seq_len, self.tag_size+2)
                masks: (batch, seq_len)
        """
        batch_size = feats.size(0)
        seq_len = feats.size(1)
        tag_size = feats.size(2)
        # print feats.view(seq_len, tag_size)
        assert(tag_size == self.tagset_size+2)
        mask = mask.transpose(1,0).contiguous()
        ins_num = seq_len * batch_size
        ## be careful the view shape, it is .view(ins_num, 1, tag_size) but not .view(ins_num, tag_size, 1)
        feats = feats.transpose(1,0).contiguous().view(ins_num,1, tag_size).expand(ins_num, tag_size, tag_size)
        ## need to consider start
        scores = feats + self.transitions.view(1,tag_size,tag_size).expand(ins_num, tag_size, tag_size)
        scores = scores.view(seq_len, batch_size, tag_size, tag_size)
        # build iter
        seq_iter = enumerate(scores)
        _, inivalues = seq_iter.next()  # bat_size * from_target_size * to_target_size
        # only need start from start_tag
        partition = inivalues[:, START_TAG, :].clone().view(batch_size, tag_size, 1)  # bat_size * to_target_size

        ## add start score (from start to all tag, duplicate to batch_size)
        # partition = partition + self.transitions[START_TAG,:].view(1, tag_size, 1).expand(batch_size, tag_size, 1)
        # iter over last scores
        for idx, cur_values in seq_iter:
            # previous to_target is current from_target
            # partition: previous results log(exp(from_target)), #(batch_size * from_target)
            # cur_values: bat_size * from_target * to_target
            
            cur_values = cur_values + partition.contiguous().view(batch_size, tag_size, 1).expand(batch_size, tag_size, tag_size)
            cur_partition = log_sum_exp(cur_values, tag_size)
            #iprint cur_partition.data
            
                # (bat_size * from_target * to_target) -> (bat_size * to_target)
            # partition = utils.switch(partition, cur_partition, mask[idx].view(bat_size, 1).expand(bat_size, self.tagset_size)).view(bat_size, -1)
            mask_idx = mask[idx, :].view(batch_size, 1).expand(batch_size, tag_size)
            
            ## effective updated partition part, only keep the partition value of mask value = 1
            masked_cur_partition = cur_partition.masked_select(mask_idx)
            ## let mask_idx broadcastable, to disable warning
            mask_idx = mask_idx.contiguous().view(batch_size, tag_size, 1)

            ## replace the partition where the maskvalue=1, other partition value keeps the same
            partition.masked_scatter_(mask_idx, masked_cur_partition)  
        # until the last state, add transition score for all partition (and do log_sum_exp) then select the value in STOP_TAG
        cur_values = self.transitions.view(1,tag_size, tag_size).expand(batch_size, tag_size, tag_size) + partition.contiguous().view(batch_size, tag_size, 1).expand(batch_size, tag_size, tag_size)
        cur_partition = log_sum_exp(cur_values, tag_size)
        final_partition = cur_partition[:, STOP_TAG]
        return final_partition, scores
    def sample(self,dist,dim=1):
        # dist is a tensor of shape (batch_size x vocab_size)
        dist=F.softmax(dist,dim=dim)
        #print("dist",dist)

        choice = torch.multinomial(dist.view(dist.size(0),-1), 1, replacement=True)
        #print("dist",choice)
        choice = choice.squeeze(1)
        return choice

    def _viterbi_decode(self, feats, mask):
        """
            input:
                feats: (batch, seq_len, self.tag_size+2)
                mask: (batch, seq_len)
            output:
                decode_idx: (batch, seq_len) decoded sequence
                path_score: (batch, 1) corresponding score for each sequence (to be implementated)
        """
        batch_size = feats.size(0)
        seq_len = feats.size(1)
        tag_size = feats.size(2)
        assert(tag_size == self.tagset_size+2)
        ## calculate sentence length for each sentence
        length_mask = torch.sum(mask, dim = 1).view(batch_size,1).long()
        ## mask to (seq_len, batch_size)
        mask = mask.transpose(1,0).contiguous()
        ins_num = seq_len * batch_size
        ## be careful the view shape, it is .view(ins_num, 1, tag_size) but not .view(ins_num, tag_size, 1)
        feats = feats.transpose(1,0).contiguous().view(ins_num, 1, tag_size).expand(ins_num, tag_size, tag_size)
        ## need to consider start
        scores = feats + self.transitions.view(1,tag_size,tag_size).expand(ins_num, tag_size, tag_size)
        scores = scores.view(seq_len, batch_size, tag_size, tag_size)

        # build iter
        seq_iter = enumerate(scores)
        ## record the position of best score
        back_points = list()
        partition_history = list()
        sample_pred = list()
        sample_score = list()

        
        ##  reverse mask (bug for mask = 1- mask, use this as alternative choice)
        # mask = 1 + (-1)*mask
        mask =  (1 - mask.long()).byte()
        _, inivalues = seq_iter.next()  # bat_size * from_target_size * to_target_size
        # only need start from start_tag
        partition = inivalues[:, START_TAG, :].clone().view(batch_size, tag_size, 1)  # bat_size * to_target_size
        partition_history.append(partition)
        # iter over last scores
        sample_partition=torch.stack([_.clone() for _ in partition]).squeeze(2)
        sample_pred.append(self.sample(sample_partition).unsqueeze(-1).unsqueeze(-1))
        sample_score.append(sample_partition)
        #print("start",F.softmax(sample_partition[1]))
        beam=list() # bat_size * from_target_size * k=to_target_size
        beam_score=Variable(torch.zeros(batch_size,tag_size).cuda())
        beam_cand_num = torch.LongTensor([_ for _ in range(tag_size)]).cuda()
        beam_cand_num1 = torch.LongTensor([_ for _ in range(tag_size)]).cuda()
        beam_choose = beam_cand_num.view(1,-1).expand(batch_size,tag_size).cuda()
        beam_cand_num = beam_cand_num.unsqueeze(0).unsqueeze(0).expand(batch_size,tag_size,tag_size).contiguous().view(batch_size,-1).unsqueeze(2).expand(batch_size,tag_size*tag_size,tag_size)
        beam_cand_num1 = beam_cand_num1.unsqueeze(0).unsqueeze(2).expand(batch_size,tag_size,tag_size).contiguous().view(batch_size,-1).unsqueeze(2).expand(batch_size,tag_size*tag_size,tag_size)
        for idx, cur_values in seq_iter:
            # previous to_target is current from_target
            # partition: previous results log(exp(from_target)), #(batch_size * from_target)
            # cur_values: batch_size * from_target * to_target.sy

            beam_cand = cur_values.gather(1,Variable(beam_choose.unsqueeze(2).expand_as(cur_values),requires_grad=False)).contiguous().view(batch_size,-1)
            beam_cand = beam_cand + beam_score.unsqueeze(2).expand(batch_size,tag_size,tag_size).contiguous().view(batch_size,-1)
            # beam[idx-1]:batch*k*1, beam_cand:batch*k*to_target
            beam_score,beam_choose_temp=beam_cand.topk(tag_size,dim=1)#k=tag_size beam_choose:batch*tag_size
            beam_from=beam_cand_num1.gather(1,beam_choose_temp.unsqueeze(1).expand_as(beam_cand_num).cuda().data)[:,0,:]
            beam_from=beam_choose.gather(1,beam_from)
            beam_choose=beam_cand_num.gather(1,beam_choose_temp.unsqueeze(1).expand_as(beam_cand_num).cuda().data)[:,0,:]
            #beam_from.masked_fill_(mask[idx].view(batch_size, 1).expand(batch_size, tag_size).data.byte(), 0) 
            beam.append(beam_from)


            cur_values = cur_values + partition.contiguous().view(batch_size, tag_size, 1).expand(batch_size, tag_size, tag_size).clone()
            ## forscores, cur_bp = torch.max(cur_values[:,:-2,:], 1) # do not consider START_TAG/STOP_TAG
            #print(sample_partition)
            #print("partition",partition)
            #print(cur_values[0])
            partition, cur_bp = torch.max(cur_values.clone(),1)


            #print(cur_bp[0])
            #print(partition[0])

            #print(sample_partition)
            #print("partition1",partition)
            #print(sample_pred[idx-1])

            #print(cur_values.gather(1,sample_pred[idx-1].expand_as(cur_values)))
            #print(sample_pred[idx-1].expand_as(cur_values))
            sample_partition=cur_values.gather(1,sample_pred[idx-1].expand_as(cur_values))[:,0,:]

            #print(F.softmax(sample_partition[1]))
            sample_pred.append(self.sample(sample_partition).unsqueeze(-1).unsqueeze(-1))#sample_partition: batch*tag

            #sample_partition=F.softmax(sample_partition,dim=1)#B*volcabulary size
            sample_score.append(sample_partition)
            #print(sample_score)
            partition_history.append(partition.clone())
            
            ## cur_bp: (batch_size, tag_size) max source score position in current tag
            ## set padded label as 0, which will be filtered in post processing
            cur_pos=cur_bp.clone()
            cur_pos.masked_fill_(mask[idx].view(batch_size, 1).expand(batch_size, tag_size), 0) 
            #if idx==1:
            #    partition.sum().backward(retain_graph=True)

            back_points.append(cur_pos)
        ### add score to final STOP_TAG
        partition_history = torch.cat(partition_history).view(seq_len, batch_size,-1).transpose(1,0).contiguous() ## (batch_size, seq_len. tag_size)
        ### get the last position for each setences, and select the last partitions using gather()
        last_position = length_mask.view(batch_size,1,1).expand(batch_size, 1, tag_size) -1
        last_partition = torch.gather(partition_history, 1, last_position).view(batch_size,tag_size,1)
        ### calculate the score from last partition to end state (and then select the STOP_TAG from it)
        last_values = last_partition.expand(batch_size, tag_size, tag_size) + self.transitions.view(1,tag_size, tag_size).expand(batch_size, tag_size, tag_size)
        _, last_bp = torch.max(last_values, 1)
        pad_zero = autograd.Variable(torch.zeros(batch_size, tag_size)).long()
        if self.gpu:
            pad_zero = pad_zero.cuda()
        back_points.append(pad_zero)
        back_points  =  torch.cat(back_points).view(seq_len, batch_size, tag_size)
        #print("******")
        path_score=partition_history
        
        ## select end ids in STOP_TAG
        pointer = last_bp[:, STOP_TAG]
        insert_last = pointer.contiguous().view(batch_size,1,1).expand(batch_size,1, tag_size)
        back_points = back_points.transpose(1,0).contiguous()
        ## move the end ids(expand to tag_size) to the corresponding position of back_points to replace the 0 values
        # print "lp:",last_position
        # print "il:",insert_last
        back_points.scatter_(1, last_position, insert_last)
        # print "bp:",back_points
        # exit(0)
        back_points = back_points.transpose(1,0).contiguous()
        ## decode from the end, padded position ids are 0, which will be filtered if following evaluation
        decode_idx = autograd.Variable(torch.LongTensor(seq_len, batch_size))
        if self.gpu:
            decode_idx = decode_idx.cuda()
        decode_idx[-1] = pointer.data
        #print("back",back_points[:,:,0])
        for idx in range(len(back_points)-2, -1, -1):
            #print(idx)
            pointer = torch.gather(back_points[idx], 1, pointer.contiguous().view(batch_size, 1))
            decode_idx[idx] = pointer.data

        
        #############################

        #############################
        #path_score = None
        decode_idx = decode_idx.transpose(1,0)
        #print("##",decode_idx)
        sample_pred=torch.stack(sample_pred, dim=1)
        #print("1",sample_score)
        #print(sample_score)
        sample_score=torch.stack(sample_score, dim=1)#B*seq*vol
        #print('mask',mask)
        #print('sample score',F.softmax(sample_score,dim=2)[1])
        m=torch.nn.LogSoftmax(dim=2)
        sample_score=-m(sample_score)#B*seq*vol
        #print('sample',sample_score)
        sample_score=sample_score.view(-1,tag_size).gather(1,sample_pred.view(-1,1)).view(batch_size,-1)#B*seq*vol
        sample_score=sample_score.mul((1 - mask.long()).transpose(1,0).float())##B*seq
        #print("sample",sample_pred)
        sample_pred=sample_pred.view(batch_size,seq_len).mul((1 - mask.long()).transpose(1,0).long())##B*seq
        #print(sample_pred)
        #sample_loss=-torch.log(sample_score)
        #print("sample score2",sample_score)

        sample_loss_sum=sample_score.sum(1)#B*1
        #print(beam_score)
        beam=torch.stack(beam,dim=1)
        #print("beam choose",beam)
        beam_sample=self.sample(beam_score)
        #beam=torch.gather(beam,2,beam_sample.data.unsqueeze(1).unsqueeze(1).expand_as(beam))[:,:,0]
        beam = beam.transpose(1,0).contiguous()

        pointer=torch.gather(beam_choose,1,beam_sample.data.unsqueeze(1).expand_as(beam_choose))[:,0]

        ## decode from the end, padded position ids are 0, which will be filtered if following evaluation
        decode_beam = autograd.Variable(torch.LongTensor(seq_len, batch_size))
        if self.gpu:
            decode_beam = decode_beam.cuda()
        #print(pointer)
        decode_beam[-1] = pointer
        #print("back",back_points[:,:,0])
        for idx in range(len(beam)-2, -1, -1):
            #print(idx)
            pointer = torch.gather(beam[idx], 1, pointer.contiguous().view(batch_size, 1))
            decode_beam[idx] = pointer
        #print(sample_pred)
        decode_beam=decode_beam.transpose(1,0)
        decode_beam=decode_beam.contiguous().view(batch_size,seq_len).mul((1 - mask.long()).transpose(1,0).long())

        m=torch.nn.LogSoftmax(dim=1)
        beam_score=-m(beam_score)
        beam_score=beam_score.gather(1,beam_sample.unsqueeze(1))#B*vol
        
        #print("sample score3",sample(beam_score))
        #print(type(beam_score))
        #print(type(decode_beam))
        #print(type(sample_pred))
        #print(type(sample_score))


        return path_score, decode_idx, decode_beam, beam_score



    def forward(self, feats):
    	path_score, best_path = self._viterbi_decode(feats)
    	return path_score, best_path
        

    def _score_sentence(self, scores, mask, tags):
        """
            input:
                scores: variable (seq_len, batch, tag_size, tag_size)
                mask: (batch, seq_len)
                tags: tensor  (batch, seq_len)
            output:
                score: sum of score for gold sequences within whole batch
        """
        # Gives the score of a provided tag sequence
        batch_size = scores.size(1)
        seq_len = scores.size(0)
        tag_size = scores.size(2)
        ## convert tag value into a new format, recorded label bigram information to index  
        new_tags = autograd.Variable(torch.LongTensor(batch_size, seq_len))
        if self.gpu:
            new_tags = new_tags.cuda()
        for idx in range(seq_len):
            if idx == 0:
                ## start -> first score
                new_tags[:,0] =  (tag_size - 2)*tag_size + tags[:,0]

            else:
                new_tags[:,idx] =  tags[:,idx-1]*tag_size + tags[:,idx]

        ## transition for label to STOP_TAG
        end_transition = self.transitions[:,STOP_TAG].contiguous().view(1, tag_size).expand(batch_size, tag_size)
        ## length for batch,  last word position = length - 1
        length_mask = torch.sum(mask, dim = 1).view(batch_size,1).long()
        ## index the label id of last word
        end_ids = torch.gather(tags, 1, length_mask - 1)

        ## index the transition score for end_id to STOP_TAG
        end_energy = torch.gather(end_transition, 1, end_ids)

        ## convert tag as (seq_len, batch_size, 1)
        new_tags = new_tags.transpose(1,0).contiguous().view(seq_len, batch_size, 1)
        ### need convert tags id to search from 400 positions of scores
        tg_energy = torch.gather(scores.view(seq_len, batch_size, -1), 2, new_tags).view(seq_len, batch_size)  # seq_len * bat_size
        ## mask transpose to (seq_len, batch_size)
        #print(mask)
        tg_energy = tg_energy.mul(mask.transpose(1,0).float()).transpose(1,0).sum(1)
        
        # ## calculate the score from START_TAG to first label
        # start_transition = self.transitions[START_TAG,:].view(1, tag_size).expand(batch_size, tag_size)
        # start_energy = torch.gather(start_transition, 1, tags[0,:])

        ## add all score together
        # gold_score = start_energy.sum() + tg_energy.sum() + end_energy.sum()
        #print("cas")
        #print(tg_energy,end_energy)
        #print(tg_energy)
        #print(end_energy)
        gold_score = tg_energy + end_energy.sum(1)
        return gold_score

    def neg_log_likelihood_loss(self, feats, mask, tags):
        # nonegative log likelihood
        batch_size = feats.size(0)
        forward_score, scores = self._calculate_PZ(feats, mask)
        gold_score = self._score_sentence(scores, mask, tags)
        # print "batch, f:", forward_score.data[0], " g:", gold_score.data[0], " dis:", forward_score.data[0] - gold_score.data[0]
        # exit(0)
        #print(forward_score)
        #print(gold_score)
        if self.average_batch:
            return (forward_score - gold_score)/batch_size
        else:
            return forward_score - gold_score

























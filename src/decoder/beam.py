from ctcdecode import CTCBeamDecoder
from src.decoder.decoder import Decoder


class BeamDecoder(Decoder):
    def __init__(self, vocab, lm_path=None, alpha=1, beta=1.5, cutoff_top_n=40, cutoff_prob=0.99, beam_width=100, num_processes=4):
        super(BeamDecoder, self).__init__(vocab)

        self._decoder = CTCBeamDecoder(vocab, lm_path, alpha, beta, cutoff_top_n, cutoff_prob, beam_width, num_processes, blank_id=0)
        self.int2char = dict([(i, c) for (i, c) in enumerate(vocab)])

    def decode(self, logits, seq_lens):
        tlogits = logits.transpose(0, 1)
        results, scores, _, out_lens = self._decoder.decode(tlogits, seq_lens)
        return self.convert_to_strings(results, out_lens)

    def convert_to_strings(self, out, seq_len):
        results = []
        for b, batch in enumerate(out):
            utterances = []
            for p, utt in enumerate(batch):
                size = seq_len[b][p]
                if size > 0:
                    transcript = ''.join(map(lambda x: self.int2char[x.item()], utt[0:size]))
                else:
                    transcript = ''
                utterances.append(transcript)
            results.append(utterances)
        return results


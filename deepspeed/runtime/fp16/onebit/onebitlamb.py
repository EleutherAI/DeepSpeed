'''
Copyright 2020 The Microsoft DeepSpeed Team
'''
import types
import torch
import numpy as np
import torch.distributed as dist
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors


class OnebitLamb(torch.optim.Optimizer):
    """Implements the 1-bit Lamb algorithm. Currently GPU-only.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups.
        lr (float, optional): learning rate. (default: 1e-3)
        freeze_step (int, optional): Number of steps for warmup (uncompressed)
            stage before we start using compressed communication. (default 100000)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square. (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability. (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        max_coeff(float, optional): maximum value of the lamb coefficient (default: 10.0)
        min_coeff(float, optional): minimum value of the lamb coefficient (default: 0.01)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False) NOT SUPPORTED in 1-bit Lamb!
        eps_inside_sqrt (boolean, optional): in the 'update parameters' step,
            adds eps to the bias-corrected second moment estimate before
            evaluating square root instead of adding it to the square root of
            second moment estimate as in the original paper. (default: False)
        cuda_aware (boolean, required): Set True if the underlying MPI implementation
            supports CUDA-Aware communication. (default: False)
        comm_backend_name (string, optional): Set to 'mpi' if needed. (default: 'nccl')
        coeff_beta (float, optional): coefficients used for computing
            running averages of lamb coefficient (default: 0.99) not that you may want to
            increase or decrease this beta depending on the freeze_step you choose:
            1/(1 - coeff_beta) should be smaller than or equal to freeze_step
        factor_max (float, optional): maximum value of scaling factor to the frozen lamb
            coefficient during compression stage (default: 4.5)
        factor_min (float, optional): maximum value of scaling factor to the frozen lamb
            coefficient during compression stage (default: 0.5)
        factor_threshold (float, optional): threshold of how much the scaling factor can
            fluctuate between steps (default: 0.1)
    .. _Large Batch Optimization for Deep Learning\: Training BERT in 76 minutes:
        https://arxiv.org/abs/1904.00962
    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """
    def __init__(self,
                 params,
                 deepspeed=None,
                 lr=1e-3,
                 freeze_step=100000,
                 bias_correction=True,
                 betas=(0.9,
                        0.999),
                 eps=1e-8,
                 eps_inside_sqrt=False,
                 weight_decay=0.,
                 max_grad_norm=0.,
                 max_coeff=10.0,
                 min_coeff=0.01,
                 amsgrad=False,
                 cuda_aware=False,
                 comm_backend_name='nccl',
                 coeff_beta=0.99,
                 factor_max=4.5,
                 factor_min=0.5,
                 factor_threshold=0.1):

        if amsgrad:
            raise RuntimeError('1-bit Lamb does not support the AMSGrad variant.')

        defaults = dict(lr=lr,
                        bias_correction=bias_correction,
                        betas=betas,
                        eps=eps,
                        weight_decay=weight_decay,
                        max_grad_norm=max_grad_norm,
                        max_coeff=max_coeff,
                        min_coeff=min_coeff)

        super(OnebitLamb, self).__init__(params, defaults)
        self.eps_mode = 0 if eps_inside_sqrt else 1
        assert (dist.is_initialized())

        self.deepspeed = deepspeed
        self.lamb_freeze_key = False
        self.initialize = False
        self.freeze_step = freeze_step
        self.cuda_aware = cuda_aware
        self.coeff_beta = coeff_beta
        self.factor_max = factor_max
        self.factor_min = factor_min
        self.factor_threshold = factor_threshold

        self.comm_backend_name = comm_backend_name

        # Empty initializer. Set handle based on the comm backend as follows.
        self.comm_backend_handle = None

        if self.comm_backend_name == 'nccl':
            assert torch.__version__.startswith("1.8."), "Please use torch 1.8 or greater to enable NCCL backend in 1-bit Adam. Alternatively, please specify 'mpi' as the 'comm_backend_name' in config file to proceed with the MPI backend"
            assert dist.is_initialized() == True, "Please initialize the torch distributed backend."
            from deepspeed.runtime.comm.nccl import NcclBackend
            self.comm_backend_handle = NcclBackend()

        elif self.comm_backend_name == 'mpi':
            from deepspeed.runtime.comm.mpi import MpiBackend
            self.comm_backend_handle = MpiBackend(cuda_aware)

        self.size = self.comm_backend_handle.size

        self.divider = int(self.size * 8 / np.gcd(self.size, 8))

        self.exp_avg_flat = []
        self.dummy_exp_avg = {}
        self.corrected_tensor_sizes = []
        self.server_chunk_sizes = []
        self.worker_errors = []
        self.server_errors = []
        self.scaling_coeffs = []

        self.lamb_coeffs = []

    def step(self, closure=None, grads=None):
        """Performs a single optimization step.
        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
            grads (list of tensors, optional): weight gradient to use for the
                optimizer update. If gradients have type torch.half, parameters
                are expected to be in type torch.float. (default: None)
        """
        loss = None
        if closure is not None:
            loss = closure()

        if grads is None:
            grads_group = [None] * len(self.param_groups)
        # backward compatibility
        # assuming a list/generator of parameter means single group
        elif isinstance(grads, types.GeneratorType):
            grads_group = [grads]
        elif type(grads[0]) != list:
            grads_group = [grads]
        else:
            grads_group = grads

        #remove the previous stats
        del self.lamb_coeffs[:]

        if self.lamb_freeze_key:
            exp_avg_last_step = []
            for group in self.param_groups:
                exp_avg_last_step.append(
                    [self.state[p]['exp_avg'].detach().clone() for p in group['params']])
            if len(self.scaling_coeffs) == 0:
                # compute the scaling_coeff for each momentum which is used to
                # reduce compression error during compressed_allreduce
                momentum_scales = []
                for group in self.param_groups:
                    momentum_scales.append([
                        (torch.norm(self.state[p]['exp_avg']) /
                         np.sqrt(torch.numel(self.state[p]['exp_avg']))).item()
                        for p in group['params']
                    ])
                united_scale = sum([sum(x) for x in momentum_scales]) / sum(
                    [len(x) for x in momentum_scales])
                for i, group in enumerate(self.param_groups):
                    self.scaling_coeffs.append([
                        united_scale / momentum_scales[i][j]
                        for j in range(len(group['params']))
                    ])

        for i, (group, grads_this_group) in enumerate(zip(self.param_groups, grads_group)):
            if grads_this_group is None:
                grads_this_group = [None] * len(group['params'])

            bias_correction = 1 if group['bias_correction'] else 0

            for j, (p, grad) in enumerate(zip(group['params'], grads_this_group)):
                if p.grad is None and grad is None:
                    continue
                if grad is None:
                    grad = p.grad.data
                if grad.is_sparse:
                    raise RuntimeError('1-bit Lamb does not support sparse gradients')

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    state['lamb_coeff_freeze'] = 0.0
                    state['last_factor'] = 1.0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p.data)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p.data)
                    state['exp_avg_sq_back'] = torch.zeros_like(p.data)

                if not self.initialize:
                    self.lamb_freeze_key = True

                exp_avg, exp_avg_sq, exp_avg_sq_back = state['exp_avg'], state['exp_avg_sq'], state['exp_avg_sq_back']
                beta1, beta2 = group['betas']
                max_coeff = group['max_coeff']
                min_coeff = group['min_coeff']

                state['step'] += 1

                if self.lamb_freeze_key is False:
                    # warmup stage, baseline Lamb optimization
                    exp_avg.mul_(beta1).add_(1 - beta1, grad)
                    exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                    if state['step'] == self.freeze_step:
                        exp_avg_sq_back.data = exp_avg_sq.detach().clone()
                    grad = None
                    if self.initialize:
                        weight_norm = p.data.pow(2).sum().sqrt()
                        update = exp_avg / (exp_avg_sq.sqrt() + group['eps'])
                        if group['weight_decay'] > 0.0:
                            update += group['weight_decay'] * p.data
                        update_norm = update.pow(2).sum().sqrt()
                        lamb_coeff = 1.0
                        if weight_norm != 0 and update_norm != 0:
                            lamb_coeff = (weight_norm / update_norm).item()
                            if lamb_coeff > max_coeff:
                                lamb_coeff = max_coeff
                            if lamb_coeff < min_coeff:
                                lamb_coeff = min_coeff
                        if lamb_coeff != 1.0:
                            state['lamb_coeff_freeze'] = self.coeff_beta * state[
                                'lamb_coeff_freeze'] + (1 - self.coeff_beta) * lamb_coeff
                        self.lamb_coeffs.append(lamb_coeff)
                        with torch.no_grad():
                            p.add_(-group['lr'] * lamb_coeff * update)
                else:
                    # compression stage, update each momentum locally, then
                    # communicate based on the compressed_allreduce below
                    if self.initialize:
                        exp_avg.mul_(beta1).add_(1 - beta1, grad)
                        exp_avg.mul_(self.scaling_coeffs[i][j])
                    grad = None

        # init fused momentum
        if len(self.exp_avg_flat) == 0:
            momentum_groups = []
            tensor_size = 0
            for group in self.param_groups:
                for p in group['params']:
                    momentum_groups.append(self.state[p]['exp_avg'])
                    tensor_size += torch.numel(p.data)
            corrected_tensor_size = tensor_size
            if tensor_size % (self.size * self.divider) != 0:
                difference = ((self.size * self.divider) - (tensor_size %
                                                            (self.size * self.divider)))
                corrected_tensor_size += difference
                self.dummy_exp_avg[0] = torch.zeros(
                    difference,
                    device=momentum_groups[0].data.device)
                momentum_groups.append(self.dummy_exp_avg[0])
            self.corrected_tensor_sizes.append(corrected_tensor_size)
            self.server_chunk_sizes.append(corrected_tensor_size // self.size)

            self.exp_avg_flat.append(
                _flatten_dense_tensors([p.detach().clone() for p in momentum_groups]))
            updated_params = _unflatten_dense_tensors(self.exp_avg_flat[0],
                                                      momentum_groups)
            for p, q in zip(momentum_groups, updated_params):
                p.data = q.data

        if self.initialize and len(self.worker_errors) == 0:
            torch.cuda.empty_cache()
            for i in range(len(self.exp_avg_flat)):
                self.worker_errors.append(
                    torch.zeros(self.corrected_tensor_sizes[i],
                                device=self.exp_avg_flat[i].device))
                self.server_errors.append(
                    torch.zeros(self.server_chunk_sizes[i],
                                device=self.exp_avg_flat[i].device))
            torch.cuda.empty_cache()

        if self.lamb_freeze_key:
            if self.size > 1:
                for i in range(len(self.exp_avg_flat)):
                    if not self.initialize:
                        torch.cuda.empty_cache()
                        self.worker_errors.append(
                            torch.zeros(self.corrected_tensor_sizes[i],
                                        device=self.exp_avg_flat[i].device))
                        self.server_errors.append(
                            torch.zeros(self.server_chunk_sizes[i],
                                        device=self.exp_avg_flat[i].device))
                        torch.cuda.empty_cache()
                        if torch.distributed.get_rank() == 0:
                            print("Cupy Buffers Initialized Successfully.")

                        self.comm_backend_handle.compressed_allreduce(
                            self.exp_avg_flat[i],
                            self.worker_errors[0],
                            self.server_errors[0],
                            self.deepspeed.local_rank)

                        if torch.distributed.get_rank() == 0:
                            print('Pop out errors', flush=True)
                        del self.worker_errors[:]
                        del self.server_errors[:]
                    else:
                        self.comm_backend_handle.compressed_allreduce(
                            self.exp_avg_flat[i],
                            self.worker_errors[i],
                            self.server_errors[i],
                            self.deepspeed.local_rank)

        if self.lamb_freeze_key and self.initialize:
            for i, group in enumerate(self.param_groups):
                bias_correction = 1 if group['bias_correction'] else 0

                for j, p in enumerate(group['params']):
                    state = self.state[p]
                    exp_avg, exp_avg_sq, exp_avg_sq_back = state['exp_avg'], state['exp_avg_sq'], state['exp_avg_sq_back']
                    beta1, beta2 = group['betas']
                    exp_avg.div_(self.scaling_coeffs[i][j])
                    if 'exp_avg_mask' in group:
                        if exp_avg.device != group['exp_avg_mask'].device:
                            group['exp_avg_mask'] = group['exp_avg_mask'].to(
                                device=exp_avg.device)
                        exp_avg.mul_(group['exp_avg_mask'])

                    grad_reconstruct = ((exp_avg - exp_avg_last_step[i][j] * beta1) /
                                        (1 - beta1))
                    exp_avg_sq_back.mul_(beta2).addcmul_(1 - beta2,
                                                         grad_reconstruct,
                                                         grad_reconstruct)
                    denom = exp_avg_sq.sqrt() + group['eps']
                    update_prelim = exp_avg / denom

                    if group['weight_decay'] > 0.0:
                        update = update_prelim + group['weight_decay'] * p.data
                    else:
                        update = update_prelim

                    lamb_coeff = 1.0
                    update_norm = update.pow(2).sum().sqrt()
                    denom_real = exp_avg_sq_back.sqrt() + group['eps']
                    factor = (denom / denom_real).max().item()
                    if group['weight_decay'] > 0.0:
                        update_ratio = min(1.0,
                                           (update_prelim.pow(2).sum().sqrt() /
                                            update_norm).item())
                        factor = factor * update_ratio + (1.0 - update_ratio)
                    if factor > self.factor_max:
                        factor = self.factor_max
                    if factor < self.factor_min:
                        factor = self.factor_min
                    if factor > state['last_factor'] * (1.0 + self.factor_threshold):
                        factor = state['last_factor'] * (1.0 + self.factor_threshold)
                    if factor < state['last_factor'] * (1.0 - self.factor_threshold):
                        factor = state['last_factor'] * (1.0 - self.factor_threshold)
                    state['last_factor'] = factor
                    lamb_coeff = state['lamb_coeff_freeze'] * factor
                    self.lamb_coeffs.append(lamb_coeff)
                    with torch.no_grad():
                        p.add_(-group['lr'] * lamb_coeff * update)
            del exp_avg_last_step[:]
            exp_avg_last_step = None

        if not self.initialize:
            self.lamb_freeze_key = False
            self.initialize = True
            print(
                f"Finished the initialization step at rank {torch.distributed.get_rank()}"
            )
            return loss

        if self.lamb_freeze_key is False:
            if state['step'] >= self.freeze_step:
                self.lamb_freeze_key = True
                self.deepspeed.enable_backward_allreduce = False

        return loss

    def state_dict(self):
        """
        Overrides state_dict() to also save 1-bit Lamb states
        """
        original_dict = super().state_dict()
        original_dict['worker_errors'] = self.worker_errors
        original_dict['server_errors'] = self.server_errors
        original_dict['scaling_coeffs'] = self.scaling_coeffs
        return original_dict

    def load_state_dict(self, state_dict):
        """
        Overrides state_dict() to reset fused momentum and load/reset 1-bit Lamb states
        """
        mask = {}
        for i, group in enumerate(self.param_groups):
            if 'exp_avg_mask' in group:
                mask[i] = group['exp_avg_mask']
        super().load_state_dict(state_dict)
        # Because at different stage exp_avg_mask may change (e.g.,
        # when loading seq 128 checkpoint for seq 512 pretraining),
        # we don't load the exp_avg_mask from the checkpoint but always
        # use the one provided in optimizer_grouped_parameters in deepspeed_train.py.
        for k, v in mask.items():
            self.param_groups[k]['exp_avg_mask'] = v
        del self.exp_avg_flat[:]
        self.dummy_exp_avg.clear()
        del self.corrected_tensor_sizes[:]
        del self.server_chunk_sizes[:]
        if self.state[self.param_groups[0]['params'][0]]['step'] >= self.freeze_step:
            if torch.distributed.get_rank() == 0:
                print(
                    "Checkpoint loaded and compression stage continues, load 1-bit Lamb states."
                )
            self.worker_errors = state_dict.pop('worker_errors')
            self.server_errors = state_dict.pop('server_errors')
            self.scaling_coeffs = state_dict.pop('scaling_coeffs')
            for i_error in range(len(self.worker_errors)):
                self.worker_errors[i_error] = self.worker_errors[i_error].to(
                    device=self.state[self.param_groups[0]['params']
                                      [0]]['exp_avg'].device)
                self.server_errors[i_error] = self.server_errors[i_error].to(
                    device=self.state[self.param_groups[0]['params']
                                      [0]]['exp_avg'].device)
        else:
            if torch.distributed.get_rank() == 0:
                print(
                    "Checkpoint loaded and warmup stage starts/continues, reset 1-bit Lamb states."
                )
            if self.lamb_freeze_key is True:
                self.lamb_freeze_key = False
                self.deepspeed.enable_backward_allreduce = True
            del self.worker_errors[:]
            del self.server_errors[:]
            del self.scaling_coeffs[:]
            for group in self.param_groups:
                for p in group['params']:
                    self.state[p]['lamb_coeff_freeze'] = 0.0
                    self.state[p]['last_factor'] = 1.0

    def get_lamb_coeffs(self):
        return self.lamb_coeffs
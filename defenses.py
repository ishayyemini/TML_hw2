import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from scipy.stats import norm
from statsmodels.stats.proportion import proportion_confint

def free_adv_train(model, data_tr, criterion, optimizer, lr_scheduler, \
                   eps, device, m=7, epochs=100, batch_size=128, dl_nw=10):
    """
    Free adversarial training, per Shafahi et al.'s work.
    Arguments:
    - model: randomly initialized model
    - data_tr: training dataset
    - criterion: loss function (e.g., nn.CrossEntropyLoss())
    - optimizer: optimizer to be used (e.g., SGD)
    - lr_scheduer: scheduler for updating the learning rate
    - eps: l_inf epsilon to defend against
    - device: device used for training
    - m: # times a batch is repeated
    - epochs: "virtual" number of epochs (equivalent to the number of 
        epochs of standard training)
    - batch_size: training batch_size
    - dl_nw: number of workers in data loader
    Returns:
    - trained model
    """
    # init data loader
    loader_tr = DataLoader(data_tr,
                           batch_size=batch_size,
                           shuffle=True,
                           pin_memory=True,
                           num_workers=dl_nw)
                           

    # init delta (adv. perturbation)
    delta = torch.zeros((batch_size, *next(iter(loader_tr))[0].shape[1:]), device=device)

    # total number of updates
    total_updates = epochs * len(loader_tr)

    # when to update lr
    scheduler_step_iters = int(np.ceil(len(data_tr)/batch_size))

    # train
    model.train()
    for epoch in range(epochs):
        for i, (inputs, targets) in enumerate(loader_tr):
            inputs, targets = inputs.to(device), targets.to(device)

            for _ in range(m):
                inputs.requires_grad = True

                # apply adv perturbation
                inputs_adv = inputs + delta[:inputs.shape[0]]
                inputs_adv = torch.clamp(inputs_adv, 0, 1)

                # forward
                outputs = model(inputs_adv)
                loss = criterion(outputs, targets)

                # backward
                optimizer.zero_grad()
                loss.backward()

                # optimize
                optimizer.step()

                # update adv perturbation
                delta[:inputs.shape[0]] = delta[:inputs.shape[0]] + eps * inputs.grad.sign()
                delta = torch.clamp(delta, -eps, eps)

            # update learning rate
            if (i + 1) % scheduler_step_iters == 0:
                lr_scheduler.step()

    # done
    return model


class SmoothedModel():
    """
    Use randomized smoothing to find L2 radius around sample x,
    s.t. the classification of x doesn't change within the L2 ball
    around x with probability >= 1-alpha.
    """

    ABSTAIN = -1

    def __init__(self, model, sigma):
        self.model = model
        self.sigma = sigma

    def _sample_under_noise(self, x, n, batch_size):
        """
        Classify input x under noise n times (with batch size 
        equal to batch_size) and return class counts (i.e., an
        array counting how many times each class was assigned the
        max confidence).
        """
        if len(x.shape) == 4:
            x = x.squeeze(0)

        # init counts
        sample_output = self.model(x.unsqueeze(0))
        counts = np.zeros(sample_output.shape[1], dtype=np.int32)

        noise = torch.randn(n, *x.shape, device=x.device) * self.sigma
        noise_batches = noise.split(batch_size)

        for noise in noise_batches:
            # classify
            with torch.no_grad():
                outputs = self.model(x + noise)
                _, preds = torch.max(outputs, dim=1)

            # update counts
            for pred in preds.cpu().numpy():
                counts[pred] += 1

        return counts


    def certify(self, x, n0, n, alpha, batch_size):
        """
        Arguments:
        - model: pytorch classification model (preferably, trained with
            Gaussian noise)
        - sigma: Gaussian noise's sigma, for randomized smoothing
        - x: (single) input sample to certify
        - n0: number of samples to find prediction
        - n: number of samples for radius certification
        - alpha: confidence level
        - batch_size: batch size to use for inference
        Outputs:
        - prediction / top class (ABSTAIN in case of abstaining)
        - certified radius (0. in case of abstaining)
        """
        
        # find prediction (top class c)
        counts_0 = self._sample_under_noise(x, n0, batch_size)
        c = np.argmax(counts_0)
        
        # compute lower bound on p_c
        counts = self._sample_under_noise(x, n, batch_size)
        p_a, _ = proportion_confint(counts[c], n, alpha=2 * alpha, method='beta')

        if p_a <= 0.5:
            # abstain
            return self.ABSTAIN, 0.

        radius = self.sigma * norm.ppf(p_a)

        # done
        return c, radius
        

class NeuralCleanse:
    """
    A method for detecting and reverse-engineering backdoors.
    """

    def __init__(self, model, dim=(1, 3, 32, 32), lambda_c=0.0005,
                 step_size=0.005, niters=2000):
        """
        Arguments:
        - model: model to test
        - dim: dimensionality of inputs, masks, and triggers
        - lambda_c: constant for balancing Neural Cleanse's objectives
            (l_class + lambda_c*mask_norm)
        - step_size: step size for SGD
        - niters: number of SGD iterations to find the mask and trigger
        """
        self.model = model
        self.dim = dim
        self.lambda_c = lambda_c
        self.niters = niters
        self.step_size = step_size
        self.loss_func = nn.CrossEntropyLoss()

    def find_candidate_backdoor(self, c_t, data_loader, device):
        """
        A method for finding a (potential) backdoor targeting class c_t.
        Arguments:
        - c_t: target class
        - data_loader: DataLoader for test data
        - device: device to run computation
        Outputs:
        - mask: 
        - trigger: 
        """
        # randomly initialize mask and trigger in [0,1]
        mask = torch.rand((self.dim[2], self.dim[3]), device=device)
        trigger = torch.rand(self.dim, device=device)

        # run self.niters of SGD to find (potential) trigger and mask - FILL ME
        optimizer = torch.optim.Adam([mask, trigger], lr=self.step_size)

        for i in range(self.niters):
            for x, _ in data_loader:
                mask.requires_grad = True
                trigger.requires_grad = True
                x = x.to(device)

                exp_mask = mask.repeat(x.shape[1], 1, 1).unsqueeze(0).expand_as(x)
                triggered_x = (1 - exp_mask) * x + exp_mask * trigger

                # forward pass
                outputs = self.model(triggered_x)
                targets = torch.full((x.shape[0],), c_t, dtype=torch.long, device=device)
                loss = self.loss_func(outputs, targets) + self.lambda_c * mask.abs().sum()

                # backward pass
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # update mask and trigger
                with torch.no_grad():
                    mask -= self.step_size * mask.grad.sign()
                    mask = torch.clamp(mask, 0, 1)

                    trigger -= self.step_size * trigger.grad.sign()
                    trigger = torch.clamp(trigger, 0, 1)

        mask = mask.repeat(3, 1, 1).to(device)

        # done
        return mask, trigger

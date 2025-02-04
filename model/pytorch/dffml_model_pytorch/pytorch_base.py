"""
Base class for PyTorch models
"""
from abc import abstractmethod
import copy
import time
import torch
import pathlib
import numpy as np
import torch.optim as optim
from torch.optim import lr_scheduler
from typing import Any, Tuple, AsyncIterator, List, Type, Dict


from dffml.record import Record
from dffml.base import config, field
from dffml.feature.feature import Feature, Features
from dffml.source.source import Sources, SourcesContext
from dffml.model.model import ModelContext, ModelNotTrained, Model

from .utils import NumpyToTensor, PyTorchLoss, CrossEntropyLossFunction


@config
class PyTorchModelConfig:
    predict: Feature = field("Feature name holding classification value")
    features: Features = field("Features to train on")
    location: pathlib.Path = field("Location where state should be saved")
    classifications: List[str] = field(
        "Options for value of classification", default=None
    )
    clstype: Type = field("Data type of classifications values", default=str)
    imageSize: int = field(
        "Common size for all images to resize and crop to", default=None
    )
    enableGPU: bool = field("Utilize GPUs for processing", default=False)
    epochs: int = field(
        "Number of iterations to pass over all records in a source", default=20
    )
    batch_size: int = field("Batch size", default=32)
    validation_split: float = field(
        "Split training data for Validation", default=0.0
    )
    patience: int = field(
        "Early stops the training if validation loss doesn't improve after a given patience",
        default=5,
    )
    loss: PyTorchLoss = field(
        "Loss Functions available in PyTorch",
        default=CrossEntropyLossFunction,
    )
    optimizer: str = field(
        "Optimizer Algorithms available in PyTorch", default="SGD"
    )
    normalize_mean: List[float] = field(
        "Mean values for normalizing Tensor image", default=None
    )
    normalize_std: List[float] = field(
        "Standard Deviation values for normalizing Tensor image", default=None
    )

    def __post_init__(self):
        if self.classifications is not None:
            self.classifications = list(
                map(self.clstype, self.classifications)
            )


class PyTorchModelContext(ModelContext):
    def __init__(self, parent):
        super().__init__(parent)

        self.features = self._applicable_features()
        self.counter = 0

    async def __aenter__(self):

        self.set_model_parameters()

        self.criterion = self.parent.config.loss.function
        self.optimizer = getattr(optim, self.parent.config.optimizer)(
            self.model_parameters, lr=0.001
        )
        self.exp_lr_scheduler = lr_scheduler.StepLR(
            self.optimizer, step_size=5, gamma=0.1
        )

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        pass

    def set_model_parameters(self):
        """
        Set model parameters to optimize according to the network
        """
        self.model_parameters = self.parent.model.parameters()

    def _applicable_features(self):
        return [name for name in self.parent.config.features.names()]

    async def dataset_generator(self, sources: Sources):
        """
        Get data from source and convert into Tensor format for further processing
        """
        self.logger.debug("Training on features: %r", self.features)
        x_cols: Dict[str, Any] = {feature: [] for feature in self.features}
        y_cols = []
        all_sources = sources.with_features(
            self.features + [self.parent.config.predict.name]
        )

        async for record in all_sources:
            for feature, results in record.features(self.features).items():
                x_cols[feature].append(np.array(results))
            y_cols.append(
                self.parent.classifications[
                    record.feature(self.parent.config.predict.name)
                ]
                if self.parent.classifications
                else record.feature(self.parent.config.predict.name)
            )
        if (len(self.features)) > 1:
            self.logger.critical(
                "Found more than one feature to train on. Only first feature will be used"
            )
        if not y_cols:
            raise ValueError("No records to train on")

        y_cols = np.array(y_cols)
        for feature in x_cols:
            x_cols[feature] = np.array(x_cols[feature])

        self.logger.info("------ Record Data ------")
        self.logger.info("x_cols:    %d", len(list(x_cols.values())[0]))
        self.logger.info("y_cols:    %d", len(y_cols))
        self.logger.info("-----------------------")

        x_cols = x_cols[self.features[0]]
        # Convert x and y data to tensors and normalize them accordingly
        dataset = NumpyToTensor(
            x_cols,
            y_cols,
            size=self.parent.config.imageSize,
            norm_mean=self.parent.config.normalize_mean,
            norm_std=self.parent.config.normalize_std,
        )

        return dataset, len(dataset)

    async def prediction_data_generator(self, data):
        dataset = NumpyToTensor(
            [data],
            size=self.parent.config.imageSize,
            norm_mean=self.parent.config.normalize_mean,
            norm_std=self.parent.config.normalize_std,
        )
        dataloader = torch.utils.data.DataLoader(dataset)
        return dataloader

    async def train(self, sources: Sources):
        dataset, size = await self.dataset_generator(sources)
        size = {
            "Training": size - int(self.parent.config.validation_split * size),
            "Validation": int(self.parent.config.validation_split * size),
        }

        # If validation_split is specified, split the data
        if self.parent.config.validation_split:
            data = dict(
                zip(
                    ["Training", "Validation"],
                    list(
                        torch.utils.data.random_split(
                            dataset, [size["Training"], size["Validation"]]
                        )
                    ),
                )
            )
            self.logger.info(
                "Data split into Training samples: {} and Validation samples: {}".format(
                    size["Training"], size["Validation"]
                )
            )
            # Combine data and perform general preprocessing like making batches, shuffling, etc.
            # Outputs an iterable variable over the data
            dataloaders = {
                x: torch.utils.data.DataLoader(
                    data[x],
                    batch_size=self.parent.config.batch_size,
                    shuffle=True,
                    num_workers=4,
                )
                for x in ["Training", "Validation"]
            }
        else:
            dataloaders = {
                "Training": torch.utils.data.DataLoader(
                    dataset,
                    batch_size=self.parent.config.batch_size,
                    shuffle=True,
                    num_workers=4,
                )
            }

        since = time.time()

        # Store initial weights of the network
        best_model_wts = copy.deepcopy(self.parent.model.state_dict())
        best_acc = 0.0

        for epoch in range(self.parent.config.epochs):
            self.logger.info(
                "Epoch {}/{}".format(epoch + 1, self.parent.config.epochs)
            )
            self.logger.info("-" * 10)

            for phase in dataloaders.keys():
                if phase == "Training":
                    self.parent.model.train()  # Set model to training phase
                else:
                    self.parent.model.eval()  # Set model to evaluation phase for validation

                running_loss = 0.0
                running_corrects = 0

                for inputs, labels in dataloaders[phase]:
                    inputs = inputs.to(self.parent.device)
                    labels = labels.to(self.parent.device)
                    self.optimizer.zero_grad()

                    # Track gradients for computing loss in the network only if the model is in training phase
                    with torch.set_grad_enabled(phase == "Training"):
                        outputs = self.parent.model(inputs)
                        if self.parent.classifications:
                            _, preds = torch.max(outputs, 1)
                        loss = self.criterion(outputs, labels)

                        # Optimize the network when in training phase
                        if phase == "Training":
                            loss.backward()
                            self.optimizer.step()

                    running_loss += loss.item() * inputs.size(0)
                    # If classification labels are specified, add up the the correct predictions
                    if self.parent.classifications:
                        running_corrects += torch.sum(preds == labels.data)

                if phase == "Training":
                    self.exp_lr_scheduler.step()

                # Calculate average accuracy and loss computed in the epoch
                epoch_loss = running_loss / size[phase]
                epoch_acc = (
                    running_corrects.double() / size[phase]
                    if self.parent.classifications
                    else 1.0 - epoch_loss
                )

                self.logger.info(
                    "{} Loss: {:.4f} Acc: {:.4f}".format(
                        phase, epoch_loss, epoch_acc
                    )
                )

                if phase == "Validation":
                    # Update the model weights if current epoch accuracy is more than the previous epoch accuracies
                    if epoch_acc >= best_acc:
                        best_acc = epoch_acc
                        best_model_wts = copy.deepcopy(
                            self.parent.model.state_dict()
                        )
                        self.counter = 0
                    else:
                        self.counter += 1
                    # To avoid overtraining, stop training after the current epoch
                    if best_acc == 1.0:
                        self.counter = self.parent.config.patience

            self.logger.info("")

            if self.counter == self.parent.config.patience:
                self.logger.info(
                    f"Early stopping: Validation Loss didn't improve for {self.counter} "
                    + "consecutive epochs OR maximum accuracy attained."
                )
                break

        time_elapsed = time.time() - since
        self.logger.info(
            "Training complete in {:.0f}m {:.0f}s".format(
                time_elapsed // 60, time_elapsed % 60
            )
        )

        if self.parent.config.validation_split:
            self.logger.info(
                "Best Validation Accuracy: {:4f}".format(best_acc)
            )
            self.parent.model.load_state_dict(best_model_wts)

    async def predict(
        self, sources: SourcesContext
    ) -> AsyncIterator[Tuple[Record, Any, float]]:
        """
        Uses trained data to make a prediction about the quality of a record.
        """
        if not self.parent.model_path.exists():
            raise ModelNotTrained("Train model before prediction.")

        self.parent.model.eval()
        async for record in sources.with_features(self.features):
            feature_data = record.features(self.features)[self.features[0]]
            predict = await self.prediction_data_generator(feature_data)
            target = self.parent.config.predict.name

            # Disable gradient calculation for prediction
            with torch.no_grad():
                for val in predict:
                    val = val.to(self.parent.device)
                    output = self.parent.model(val)

                    if self.parent.classifications:
                        prob = torch.nn.functional.softmax(output, dim=1)
                        confidence, prediction_value = prob.topk(1, dim=1)
                        record.predicted(
                            target,
                            self.parent.cids[prediction_value.item()],
                            confidence,
                        )
                    else:
                        confidence = 1.0 - self.criterion(val, output).item()
                        record.predicted(target, output, confidence)

            yield record


class PyTorchModel(Model):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.model = None
        if self.config.classifications:
            self.cids = self._mkcids(self.config.classifications)
            self.classifications = self._classifications(self.cids)
        else:
            self.classifications = None

        if all([self.config.enableGPU, torch.cuda.is_available()]):
            self.device = torch.device("cuda:0")
            self.logger.info("Using CUDA")
        else:
            self.device = torch.device("cpu")

    @abstractmethod
    def createModel(self):
        """
        This should be implemented by the child class inheriting from
        this class.
        """
        raise NotImplementedError(
            "Can't use createModel method from PyTorchModel"
        )

    @property
    def base_path(self):
        return (
            self.config.location
            if not hasattr(self, "temp_dir")
            else self.temp_dir
        )

    @property
    def model_path(self):
        return self.base_path / "model.pt"

    def _classifications(self, cids):
        """
        Map classifications to numeric values
        """
        classifications = {value: key for key, value in cids.items()}
        self.logger.debug(
            "classifications(%d): %r", len(classifications), classifications
        )
        return classifications

    def _mkcids(self, classifications):
        """
        Create an index, possible classification mapping and sort the list of
        classifications first.
        """
        cids = dict(
            zip(range(0, len(classifications)), sorted(classifications))
        )
        self.logger.debug("cids(%d): %r", len(cids), cids)
        return cids

    async def __aenter__(self) -> "PyTorchModel":
        await super().__aenter__()
        if self.model_path.exists():
            self.logger.info(f"Using saved model from {self.model_path}")
            self.model = torch.load(self.model_path)
        else:
            self.model = self.createModel()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self.model:
            # Save the model at the specified path
            torch.save(self.model, self.model_path)
        await super().__aexit__(exc_type, exc_value, traceback)

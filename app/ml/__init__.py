"""Machine-learning package: BRFSS data loading, risk-model training and inference.

Public entry points:
    * :func:`app.ml.train.train_risk_model` - train, calibrate and save the model.
    * :class:`app.ml.predict.RiskModel` - load the saved model and score a user profile.
"""
